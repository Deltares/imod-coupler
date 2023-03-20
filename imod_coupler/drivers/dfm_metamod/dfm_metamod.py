""" MetaMod: the coupling between MetaSWAP and MODFLOW 6

description:

"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Dict

import numpy as np
import scipy.sparse as spr
import tomli
from loguru import logger
from numpy import float_, int_
from numpy.typing import NDArray
from scipy.sparse import csr_matrix, dia_matrix
from xmipy import XmiWrapper

from imod_coupler.config import BaseConfig
from imod_coupler.drivers.dfm_metamod.config import Coupling, DfmMetaModConfig
from imod_coupler.drivers.dfm_metamod.dfm_metamod_output_labels import outputlabels
from imod_coupler.drivers.dfm_metamod.dfm_wrapper import DfmWrapper
from imod_coupler.drivers.dfm_metamod.exchange import (
    exchange_balance_1d,
    exchange_balance_2d,
)
from imod_coupler.drivers.dfm_metamod.mapping import Mapping
from imod_coupler.drivers.dfm_metamod.mf6_wrapper import Mf6Wrapper
from imod_coupler.drivers.dfm_metamod.msw_wrapper import MswWrapper
from imod_coupler.drivers.driver import Driver
from imod_coupler.utils import Operator, create_mapping


class DfmMetaMod(Driver):
    """The driver coupling DFLOW-FM, MetaSWAP and MODFLOW 6"""

    name: str = "dfm_metamod"  # name of the driver
    base_config: BaseConfig  # the parsed information from the configuration file
    dfm_metamod_config: DfmMetaModConfig  # the parsed information from the configuration file specific to MetaMod
    coupling: Coupling  # the coupling information

    timing: bool  # true, when timing is enabled
    mf6: Mf6Wrapper  # the MODFLOW 6 XMI kernel
    msw: MswWrapper  # the MetaSWAP XMI kernel
    dfm: DfmWrapper  # the dflow-fm BMI kernel

    delt_mf6: float  # time step from MODFLOW 6 (leading)
    delt_msw_dflow: float  # timestep of fast proceses in MetaSWAP
    number_substeps_per_modflowstep: float  # number of subtimesteps between metaSWAP-DFLOW in a single MF6 timestep

    # sparse matrices used for  modflow-dflow exchanges
    map_active_mod_dflow1d: dict[str, csr_matrix]
    map_passive_mod_dflow1d: dict[str, csr_matrix]
    # masks used for  modflow-dflow exchanges
    mask_active_mod_dflow1d: dict[str, NDArray[np.int_]]
    mask_passive_mod_dflow1d: dict[str, NDArray[np.int_]]
    # dictionary with mapping tables for mod=>msw coupling
    map_mod2msw: Dict[str, csr_matrix]
    # dictionary with mapping tables for msw=>mod coupling
    map_msw2mod: Dict[str, csr_matrix]
    # dict. with mask arrays for mod=>msw coupling
    mask_mod2msw: Dict[str, NDArray[Any]]
    # dict. with mask arrays for msw=>mod coupling
    mask_msw2mod: Dict[str, NDArray[Any]]
    # dictionary with mapping tables for msw-dflow coupling
    map_msw_dflow1d: Dict[str, csr_matrix]
    map_msw_dflow2d: Dict[str, csr_matrix]
    # dictionary with mask arrays for msw-dflow coupling
    mask_msw_dflow1d: Dict[str, NDArray[Any]]
    mask_msw_dflow2d: Dict[str, NDArray[Any]]

    # tolerance for time-related comparisons
    time_eps = 1e-5

    def __init__(
        self, base_config: BaseConfig, config_dir: Path, driver_dict: Dict[str, Any]
    ):
        """Constructs the `DfmMetaMod` object"""
        self.base_config = base_config
        self.dfm_metamod_config = DfmMetaModConfig(config_dir, **driver_dict)
        self.coupling = self.dfm_metamod_config.coupling[
            0
        ]  # Adapt as soon as we have multimodel support

    def initialize(self) -> None:
        self.mf6 = Mf6Wrapper(
            self.dfm_metamod_config.kernels.modflow6.dll,
            self.dfm_metamod_config.kernels.modflow6.dll_dep_dir,
            self.dfm_metamod_config.kernels.modflow6.work_dir,
            self.base_config.timing,
        )
        self.msw = MswWrapper(
            self.dfm_metamod_config.kernels.metaswap.dll,
            self.dfm_metamod_config.kernels.metaswap.dll_dep_dir,
            self.dfm_metamod_config.kernels.metaswap.work_dir,
            self.base_config.timing,
        )
        # ================
        # modifying the path here should not be necessary
        os.environ["PATH"] = (
            os.path.dirname(self.dfm_metamod_config.kernels.dflowfm.dll)
            + os.pathsep
            + os.environ["PATH"]
        )
        # ================
        mdu_name = self.coupling.dict()["dfm_model"]
        dflowfm_input = self.dfm_metamod_config.kernels.dflowfm.work_dir.joinpath(
            mdu_name
        )
        self.dfm = DfmWrapper(engine="dflowfm", configfile=dflowfm_input)

        # Print output to stdout
        self.mf6.set_int("ISTDOUTTOFILE", 0)
        self.mf6.initialize()
        self.msw.initialize()
        self.msw.initialize_surface_water_component()
        self.dfm.initialize()
        self.get_array_dims()
        self.mapping = Mapping(
            self.coupling, self.msw.working_directory, self.array_dims
        )
        self.set_mapping()
        self.log_version()
        self.exchange_balans_1d = exchange_balance_1d(self.array_dims["dfm_1d"])
        self.exchange_balans_2d = exchange_balance_2d(self.array_dims["dfm_2d"])

        output_toml_file = self.coupling.dict()["output_config_file"]
        with open(output_toml_file, "rb") as f:
            toml_dict = tomli.load(f)

        self.exchange_logger = ExchangeCollector(toml_dict)

    def log_version(self) -> None:
        logger.info(f"MODFLOW version: {self.mf6.get_version()}")
        logger.info(f"MetaSWAP version: {self.msw.get_version()}")
        logger.info(f"Dflow FM version: version fetching not implemented in BMI")

    def set_mapping(self) -> None:
        storage_conversion = self.mf6_storage_conversion_term()
        self.map_mod_msw, self.mask_mod_msw = self.mapping.mapping_mf_msw(
            storage_conversion
        )
        (
            self.map_active_mod_dflow1d,
            self.mask_active_mod_dflow1d,
        ) = self.mapping.mapping_active_mf_dflow1d()
        (
            self.map_passive_mod_dflow1d,
            self.mask_passive_mod_dflow1d,
        ) = self.mapping.mapping_passive_mf_dflow1d()
        self.map_msw_dflow1d, self.mask_msw_dflow1d = self.mapping.mapping_msw_dflow1d()
        self.map_msw_dflow2d, self.mask_msw_dflow2d = self.mapping.mapping_msw_dflow2d()

    def update(self) -> None:
        # heads from modflow to MetaSWAP
        self.exchange_mod2msw()

        # we cannot set the timestep (yet) in Modflow
        # -> set to the (dummy) value 0.0 for now
        t_begin = self.get_current_time()
        self.mf6.prepare_time_step(0.0)

        self.delt_mf6 = self.mf6.get_time_step()
        self.delt_msw_dflow = self.msw.get_sw_time_step()
        self.number_substeps_per_modflowstep = int(self.delt_mf6 / self.delt_msw_dflow)
        self.msw.prepare_time_step(self.delt_mf6)

        # initialise water balance 1d + 2d
        self.exchange_balans_1d.reset()
        self.exchange_balans_2d.reset()

        # stage from dflow 1d to modflow active coupled riv
        self.exchange_stage_1d_dfm2mf6()

        # flux from modflow active coupled riv to water balance 1d
        self.exchange_flux_riv_active_mf62dfm()

        # flux from modflow passive coupled riv to water balance 1d
        self.exchange_flux_riv_passive_mf62dfm()

        # flux from modflow passive coupled drn to water balance 1d
        self.exchange_flux_drn_passive_mf62dfm()

        # sub timestepping between metaswap and dflow
        subtimestep_endtime = t_begin
        for idtsw in range(self.number_substeps_per_modflowstep):
            subtimestep_endtime += self.delt_msw_dflow

            # initial 2d stage from dflow 2d to msw
            self.exchange_stage_2d_dfm2msw()

            # initiate surface water timestep
            self.msw.start_surface_water_time_step(idtsw)

            # flux from metaswap ponding to water balance 1d
            self.exchange_ponding_msw2dflow1d()

            # flux from metaswap sprinkling to water balance 1d
            self.exchange_sprinkling_msw2dflow1d()

            # exchange ponding msw to water balance 2d
            self.exchange_ponding_msw2dflow2d()

            # exchange water balance 1d to dlfow 1d
            self.exchange_balans_1d.sum_demand()
            self.exchange_balans_2dfm(self.exchange_balans_1d.demand["sum"])

            # get cummelative flux before dfm-run
            q_dflow_before_run_dflow_1d = self.dfm.get_cumulative_fluxes_1d_nodes()
            q_dflow_before_run_dflow_2d = self.dfm.get_cumulative_fluxes_2d_nodes()

            # run dflow
            while (
                self.dfm.get_current_time()
                < days_to_seconds(subtimestep_endtime) - self.time_eps
            ):
                self.dfm.update()

            # get cummelative flux after dfm-run
            q_dflow_after_run_dflow_1d = self.dfm.get_cumulative_fluxes_1d_nodes()
            q_dflow1_after_run_dflow_2d = self.dfm.get_cumulative_fluxes_2d_nodes()

            # calculate realised volumes by dflow
            q_dflow_realised_1d = (
                q_dflow_after_run_dflow_1d - q_dflow_before_run_dflow_1d
            )
            q_dflow_realised_2d = (
                q_dflow1_after_run_dflow_2d - q_dflow_before_run_dflow_2d
            )
            self.exchange_balans_1d.compute_realised(q_dflow_realised_1d)
            self.exchange_balans_2d.compute_realised(q_dflow_realised_2d)

            # exchange realised values 1d to metaswap before finish of surface water time-step
            self.exchange_sprinkling_dflow1d2msw(
                self.exchange_balans_1d.realised["dflow1d_flux2sprinkling_msw"]
            )

            # exchange realised values 2d to metaswap
            self.exchange_ponding_dflow2d2msw(
                self.exchange_balans_2d.realised["dflow2d-flux2msw-ponding"]
            )

            # exchange 2d stage to msw, so it can finish the sw-timestep (now stage at the start of next timestep)
            self.exchange_stage_2d_dfm2msw()

            self.msw.finish_surface_water_time_step(idtsw)

        # exchange correction flux to MF6
        self.exchange_correction_dflow2mf6(
            self.exchange_balans_1d.realised["dflow1d_flux2mf-riv_negative"]
        )

        # convergence loop modflow-metaswap
        self.mf6.prepare_solve(1)
        for kiter in range(1, self.mf6.max_iter() + 1):
            has_converged = self.do_iter_mf_msw(1)
            if has_converged:
                logger.debug(f"MF6-MSW converged in {kiter} iterations")
                break
        self.mf6.finalize_solve(1)

        self.mf6.finalize_time_step()
        # self.msw_time = self.mf6.get_current_time() -> zie definitie
        self.msw.finalize_time_step()

    def finalize(self) -> None:
        self.mf6.finalize()
        self.msw.finalize()
        self.dfm.finalize()
        self.exchange_logger.finalize()

    def get_current_time(self) -> float:
        return self.mf6.get_current_time()

    def get_end_time(self) -> float:
        return self.mf6.get_end_time()

    def get_array_dims(self) -> None:
        array_dims = {
            "msw_storage": self.msw.get_storage_ptr().size,
            "msw_head": self.msw.get_head_ptr().size,
            "msw_volume": self.msw.get_volume_ptr().size,
            "msw_sw_sprinkling": self.msw.get_surfacewater_sprinking_demand_ptr().size,
            "msw_sw_ponding": self.msw.get_surfacewater_sprinking_demand_ptr().size,
            "mf6_storage": self.mf6.get_storage(self.coupling.mf6_model).size,
            "mf6_head": self.mf6.get_head(self.coupling.mf6_model).size,
            "mf6_recharge": self.mf6.get_recharge(
                self.coupling.mf6_model, self.coupling.mf6_msw_recharge_pkg
            ).size,
            "mf6_riv_active": self.mf6.get_river_flux_estimate(
                self.coupling.mf6_model, self.coupling.mf6_river_active_pkg
            ).size,
            "mf6_riv_passive": self.mf6.get_river_drain_flux(
                self.coupling.mf6_model, self.coupling.mf6_river_passive_pkg
            ).size,
            "mf6_drn": self.mf6.get_river_drain_flux(
                self.coupling.mf6_model, self.coupling.mf6_drain_pkg
            ).size,
            "dfm_1d": self.dfm.get_number_1d_nodes(),
            "dfm_2d": self.dfm.get_number_2d_nodes(),
        }

        if self.coupling.enable_sprinkling:
            assert self.coupling.mf6_msw_well_pkg is not None
            array_dims["mf6_sprinkling_wells"] = self.mf6.get_sprinkling(
                self.coupling.mf6_model, self.coupling.mf6_msw_well_pkg
            ).size
        self.array_dims = array_dims

    def mf6_storage_conversion_term(self) -> dia_matrix:
        """calculated storage conversion terms to use for exchange from metaswap to mf6

        Args:
        none

        Returns:
            conversion_matrix (NDArray[float_]): array with conversion terms
        """

        if self.mf6.has_sc1(self.coupling.mf6_model):
            conversion_terms = 1.0 / self.mf6.get_area(self.coupling.mf6_model)
        else:
            conversion_terms = 1.0 / (
                self.mf6.get_area(self.coupling.mf6_model)
                * (
                    self.mf6.get_top(self.coupling.mf6_model)
                    - self.mf6.get_bot(self.coupling.mf6_model)
                )
            )

        conversion_matrix = dia_matrix(
            (conversion_terms, [0]),
            shape=(
                self.mf6.get_area(self.coupling.mf6_model).size,
                self.mf6.get_area(self.coupling.mf6_model).size,
            ),
            dtype=float,
        )
        return conversion_matrix

    def matrix_product(
        self,
        flux: NDArray[Any],
        water_balance: dict[str, NDArray[float_]],
        coupling: dict[str, csr_matrix],
        mask: dict[str, NDArray[int_]],
        exchange_type: str,
    ) -> None:
        water_balance[exchange_type][:] = (
            mask[exchange_type][:] * water_balance[exchange_type][:]
            + coupling[exchange_type].dot(flux)[:]
        )
        self.exchange_logger.log_exchange(
            exchange_type + "_input", flux, self.get_current_time()
        )
        self.exchange_logger.log_exchange(
            exchange_type + "_output",
            self.exchange_balans.demand[exchange_type][:],
            self.get_current_time(),
        )

    def exchange_balans_2dfm(self, flux2dflow: NDArray[float_]) -> None:
        self.dfm.set_1d_river_fluxes(flux2dflow)

    def exchange_stage_1d_dfm2mf6(self) -> None:
        """
        From DFM to MF6.
        Waterlevels in the 1D-rivers at the beginning of the mf6-timestep. (T=t)
        Should be set as the MF6 river stages.
        MF6 unit: meters above MF6's reference plane
        DFM unit: ?
        """
        dfm_water_levels = self.dfm.get_waterlevels_1d()
        mf6_river_stage = self.mf6.get_river_stages(
            self.coupling.mf6_model, self.coupling.mf6_river_active_pkg
        )

        updated_river_stage = (
            self.mask_active_mod_dflow1d["dflow1d2mf-riv_stage"][:] * mf6_river_stage[:]
            + self.map_active_mod_dflow1d["dflow1d2mf-riv_stage"].dot(dfm_water_levels)[
                :
            ]
        )

        self.mf6.set_river_stages(
            self.coupling.mf6_model,
            self.coupling.mf6_river_active_pkg,
            updated_river_stage,
        )

        self.exchange_logger.log_exchange(
            outputlabels["dflow_river_stage"], dfm_water_levels, self.get_current_time()
        )
        self.exchange_logger.log_exchange(
            outputlabels["modflow_updated_river_stage"],
            updated_river_stage,
            self.get_current_time(),
        )

    def exchange_stage_2d_dfm2msw(self) -> None:
        """
        Sets dfm2d-stage to msw.

        MSW unit: m+DEM (depth)
        DFM unit: m+NAP
        """
        dfm_water_levels = self.dfm.get_waterlevels_2d()
        dfm_bed_level = self.dfm.get_bed_level_2d()
        dfm_water_depth = dfm_bed_level
        condition = dfm_water_levels > (dfm_bed_level + np.double(0.001))
        dfm_water_depth[condition] = dfm_water_levels[condition]

        msw_water_levels_ptr = self.msw.get_ponding_level_2d_ptr()

        msw_water_levels_ptr = (
            self.mask_msw_dflow2d["dflow2d_stage2msw-ponding"][:]
            * msw_water_levels_ptr[:]
            + self.map_msw_dflow2d["dflow2d_stage2msw-ponding"].dot(dfm_water_depth)[:]
        )

    def exchange_ponding_msw2dflow2d(self) -> None:
        ponding_msw_m3dtsw = self.msw.get_surfacewater_ponding_allocation_ptr()
        ponding_msw_m3s = ponding_msw_m3dtsw / days_to_seconds(self.delt_msw_dflow)

        self.matrix_product(
            ponding_msw_m3s,
            self.exchange_balans_2d.demand,
            self.map_msw_dflow2d,
            self.mask_msw_dflow2d,
            "msw-ponding2dflow2d_flux",
        )
        # for calculating the realised ponding volume, the flux need to be split up in positive and negative values
        # positive values means runoff from msw to dflow
        # negative values mean runon from dflow to msw
        ponding_msw_m3s_conditions = np.copy(ponding_msw_m3s)
        ponding_msw_m3s_conditions[ponding_msw_m3s < 0] = 0.0
        self.exchange_balans_2d.demand["msw-ponding2dflow2d_flux_positive"][:] = (
            self.mask_msw_dflow2d["msw-ponding2dflow2d_flux"][:]
            * self.exchange_balans_2d.demand["msw-ponding2dflow2d_flux_positive"][:]
            + self.map_msw_dflow2d["msw-ponding2dflow2d_flux"].dot(
                ponding_msw_m3s_conditions
            )[:]
        )
        ponding_msw_m3s_conditions = np.copy(ponding_msw_m3s)
        ponding_msw_m3s_conditions[ponding_msw_m3s > 0] = 0.0
        self.exchange_balans_2d.demand["msw-ponding2dflow2d_flux_negative"][:] = (
            self.mask_msw_dflow2d["msw-ponding2dflow2d_flux"][:]
            * self.exchange_balans_2d.demand["msw-ponding2dflow2d_flux_negative"][:]
            + self.map_msw_dflow2d["msw-ponding2dflow2d_flux"].dot(
                ponding_msw_m3s_conditions
            )[:]
        )

    def exchange_ponding_dflow2d2msw(
        self, dfm_flux_2d_realised: NDArray[np.float_]
    ) -> None:
        dfm_flux_2d_realised_m3dtsw = dfm_flux_2d_realised * days_to_seconds(
            self.delt_msw_dflow
        )
        ponding_msw = self.msw.get_surfacewater_ponding_realised_ptr()
        ponding_msw = (
            self.mask_msw_dflow2d["dflow2d_flux2msw-ponding"][:] * ponding_msw[:]
            + self.map_msw_dflow2d["dflow2d_flux2msw-ponding"].dot(
                dfm_flux_2d_realised_m3dtsw
            )[:]
        )

    def exchange_flux_riv_active_mf62dfm(self) -> None:
        """
        From MF6 to DFM.
        requested infiltration/drainage in the coming MF6 timestep for the 1D-rivers,
        estimated based on the MF6 groundwater levels and DFM water levels at T =t
        (so at the beginning of the timestep)

        The dflow 1d flux is set to zero, since this is the first call of the timestep

        MF6 unit: m3/d
        DFM unit: m3/s
        """

        # conversion from (-)m3/dtgw to (+)m3/s
        mf6_river_aquifer_flux_day = self.mf6.get_river_flux_estimate(
            self.coupling.mf6_model, self.coupling.mf6_river_active_pkg
        )
        mf6_river_aquifer_flux_sec = -mf6_river_aquifer_flux_day / days_to_seconds(
            self.delt_mf6
        )
        self.matrix_product(
            mf6_river_aquifer_flux_sec,
            self.exchange_balans_1d.demand,
            self.map_active_mod_dflow1d,
            self.mask_active_mod_dflow1d,
            "mf-riv2dflow1d_flux",
        )
        # for calculating the correction flux, the flux need to be split up in positive and negative values
        # since the sign is already swapped, positive values means drainage from mf6 to dflow and
        # negative values mean infiltration from dflow to MF6
        mf6_river_aquifer_flux_sec_conditions = np.copy(mf6_river_aquifer_flux_sec)
        mf6_river_aquifer_flux_sec_conditions[mf6_river_aquifer_flux_sec < 0] = 0.0
        self.exchange_balans_1d.demand["mf-riv2dflow1d_flux_positive"][:] = (
            self.mask_active_mod_dflow1d["mf-riv2dflow1d_flux"][:]
            * self.exchange_balans_1d.demand["mf-riv2dflow1d_flux"][:]
            + self.map_active_mod_dflow1d["mf-riv2dflow1d_flux"].dot(
                mf6_river_aquifer_flux_sec_conditions
            )[:]
        )

        mf6_river_aquifer_flux_sec_conditions = np.copy(mf6_river_aquifer_flux_sec)
        mf6_river_aquifer_flux_sec_conditions[mf6_river_aquifer_flux_sec > 0] = 0.0
        self.exchange_balans_1d.demand["mf-riv2dflow1d_flux_negative"][:] = (
            self.mask_active_mod_dflow1d["mf-riv2dflow1d_flux"][:]
            * self.exchange_balans_1d.demand["mf-riv2dflow1d_flux"][:]
            + self.map_active_mod_dflow1d["mf-riv2dflow1d_flux"].dot(
                mf6_river_aquifer_flux_sec_conditions
            )[:]
        )

    def exchange_ponding_msw2dflow1d(self) -> None:
        # conversion from (+)m3/dtsw to (+)m3/s
        msw_ponding_volume = self.msw.get_surfacewater_ponding_allocation_ptr()
        msw_ponding_flux_sec = msw_ponding_volume / days_to_seconds(self.delt_msw_dflow)

        self.matrix_product(
            msw_ponding_flux_sec,
            self.exchange_balans_1d.demand,
            self.map_msw_dflow1d,
            self.mask_msw_dflow1d,
            "msw-ponding2dflow1d_flux",
        )

    def exchange_sprinkling_msw2dflow1d(self) -> None:
        # conversion from (+)m3/dtsw to (+)m3/s
        msw_sprinkling_demand = self.msw.get_surfacewater_sprinking_demand_ptr()
        msw_sprinkling_flux_sec = msw_sprinkling_demand / days_to_seconds(
            self.delt_msw_dflow
        )

        self.matrix_product(
            msw_sprinkling_flux_sec,
            self.exchange_balans_1d.demand,
            self.map_msw_dflow1d,
            self.mask_msw_dflow1d,
            "msw-sprinkling2dflow1d_flux",
        )

    def exchange_sprinkling_dflow1d2msw(
        self, sprinkling_dflow: NDArray[np.float_]
    ) -> None:
        # conversion from (+)m3/s to (+)m3/dtsw
        sprinkling_dflow_dtsw = sprinkling_dflow * days_to_seconds(self.delt_msw_dflow)
        # get the realised pointer
        sprinkling_msw = self.msw.get_surfacewater_sprinking_realised_ptr()
        # set pointer
        sprinkling_msw = (
            self.mask_msw_dflow1d["dflow1d_flux2sprinkling_msw"][:] * sprinkling_msw[:]
            + self.map_msw_dflow1d["dflow1d_flux2sprinkling_msw"].dot(
                sprinkling_dflow_dtsw
            )[:]
        )

    def exchange_flux_riv_passive_mf62dfm(self) -> None:
        """
        From MF6 to DFM.
        Calculated RIV2 drainage flux from MF6 to DFLOW 1D. The flux is for now added to the DFLOW vector.
        The dflow vector wil be emptied for every timestep by the function exchange_flux_riv_active_mf62dfm.
        TODO: add fluxes to a shared waterbalanse in our driver to create a shared water volume that can be used in the msw-dflow timestepping

        MF6 unit: m3/d
        DFM unit: m3/s
        """
        mf6_riv2_flux = self.mf6.get_river_drain_flux(
            self.coupling.mf6_model, self.coupling.mf6_river_passive_pkg
        )
        # conversion from (-)m3/dtgw to (+)m3/s
        mf6_riv2_flux_sec = -mf6_riv2_flux / days_to_seconds(self.delt_mf6)

        self.matrix_product(
            mf6_riv2_flux_sec,
            self.exchange_balans_1d.demand,
            self.map_passive_mod_dflow1d,
            self.mask_passive_mod_dflow1d,
            "mf-riv2dflow1d_passive_flux",
        )

    def exchange_flux_drn_passive_mf62dfm(self) -> None:
        """
        From MF6 to DFM.
        Calculated DRN drainage flux from MF6 to DFLOW 1D.The flux is for now added to the DFLOW vector.
        The dflow vector wil be emptied for every timestep by the function exchange_flux_riv_active_mf62dfm.
        TODO: add fluxes to a shared waterbalanse in our driver to create a shared water volume that can be used in the msw-dflow timestepping

        MF6 unit: m3/d
        DFM unit: m3/s
        """
        mf6_drn_flux = self.mf6.get_river_drain_flux(
            self.coupling.mf6_model, self.coupling.mf6_drain_pkg
        )
        # conversion from (+)m3/dtgw to (+)m3/s
        mf6_drn_flux_sec = -mf6_drn_flux / days_to_seconds(self.delt_mf6)

        self.matrix_product(
            mf6_drn_flux_sec,
            self.exchange_balans_1d.demand,
            self.map_passive_mod_dflow1d,
            self.mask_passive_mod_dflow1d,
            "mf-drn2dflow1d_flux",
        )

    def exchange_correction_dflow2mf6(self, qdfm_realised: NDArray[float_]) -> None:
        """
        From DFM to MF6
        the drainage/inflitration flux to the 1d rivers as realised by DFM is passed to
        mf6 as a correction
        """

        # mf6 riv-active demand, dims=mf6
        qmf6_demand = self.mf6.get_river_flux_estimate(
            self.coupling.mf6_model, self.coupling.mf6_river_active_pkg
        )
        qmf6_demand_sec = -qmf6_demand / days_to_seconds(self.delt_mf6)

        # mf6 riv-active demand, dims=dfm
        qdfm_demand = self.exchange_balans_1d.demand["mf-riv2dflow1d_flux"]

        qmf_corr = self.mapping.calc_correction(
            self.map_active_mod_dflow1d["mf-riv2dflow1d_flux"],
            qmf6_demand_sec,
            qdfm_demand,
            qdfm_realised,
        )

        assert self.coupling.mf6_msw_well_pkg
        self.mf6.set_well_flux(
            self.coupling.mf6_model, self.coupling.mf6_wel_correction_pkg, qmf_corr
        )

    def exchange_msw2mod(self) -> None:
        """
        Exchange from Metaswap to MF6

        1- Change of storage-coefficient from MetaSWAP to MF6
        2- Recharge from MetaSWAP to MF6
        3- Sprinkling request from MetaSWAP to MF6

        """

        self.mf6.get_storage(self.coupling.mf6_model)[:] = (
            self.mask_mod_msw["msw2mf_storage"][:]
            * self.mf6.get_storage(self.coupling.mf6_model)[:]
            + self.map_mod_msw["msw2mf_storage"].dot(self.msw.get_storage_ptr())[:]
        )

        # Divide recharge and extraction by delta time
        tled = 1 / self.delt_mf6
        self.mf6.get_recharge(
            self.coupling.mf6_model, self.coupling.mf6_msw_recharge_pkg
        )[:] = (
            self.mask_mod_msw["msw2mod_recharge"][:]
            * self.mf6.get_recharge(
                self.coupling.mf6_model, self.coupling.mf6_msw_recharge_pkg
            )[:]
            + tled
            * self.map_mod_msw["msw2mod_recharge"].dot(self.msw.get_volume_ptr())[:]
        )

        if self.coupling.enable_sprinkling:
            assert self.coupling.mf6_msw_well_pkg is not None
            self.mf6.get_sprinkling(
                self.coupling.mf6_model, self.coupling.mf6_msw_well_pkg
            )[:] = (
                self.mask_mod_msw["msw2mf6_sprinkling"][:]
                * self.mf6.get_sprinkling(
                    self.coupling.mf6_model, self.coupling.mf6_msw_well_pkg
                )[:]
                + tled
                * self.map_mod_msw["msw2mf6_sprinkling"].dot(self.msw.get_volume_ptr())[
                    :
                ]
            )

    def exchange_mod2msw(self) -> None:
        """
        Exchange from MF6 to Metaswap

        1- Exchange of head from MF6 to MetaSWAP
        """

        time = self.get_current_time()
        self.msw.get_head_ptr()[:] = (
            self.mask_mod_msw["mod2msw_head"][:] * self.msw.get_head_ptr()[:]
            + self.map_mod_msw["mod2msw_head"].dot(
                self.mf6.get_head(self.coupling.mf6_model)
            )[:]
        )
        self.exchange_logger.log_exchange(
            outputlabels["metaswap_head_in"], self.msw.get_head()[:], time
        )

    def report_timing_totals(self) -> None:
        total_mf6 = self.mf6.report_timing_totals()
        total_msw = self.msw.report_timing_totals()
        total = total_mf6 + total_msw
        logger.info(f"Total elapsed time in numerical kernels: {total:0.4f} seconds")

    def do_iter_mf_msw(self, sol_id: int) -> bool:
        """Execute a single iteration"""
        self.msw.prepare_solve(0)
        self.msw.solve(0)
        self.exchange_msw2mod()
        has_converged = self.mf6.solve(sol_id)
        self.exchange_mod2msw()
        self.msw.finalize_solve(0)
        return has_converged


def days_to_seconds(time: float) -> float:
    return time * 86400
