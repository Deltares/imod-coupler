from collections.abc import Callable
from pathlib import Path
from typing import Any, Dict, List, NamedTuple

import geopandas as gpd
import imod
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
import pytest
import tomli_w
import xarray as xr
from imod.msw import MetaSwapModel
from primod import RibaModActiveDriverCoupling, RibaModPassiveDriverCoupling
from primod.ribametamod import RibaMetaMod
from pytest_cases import parametrize_with_cases
from test_ribamod_cases import create_basin_definition

from imod_coupler.drivers.ribametamod.exchange import ExchangeBalance


class exchange_output:
    exchanges: list[Any]
    output_dir: Path

    def __init__(self, exchanges: list[Any]) -> None:
        self.exchanges = exchanges

    def write_toml(
        self,
        output_dir: str | Path,
    ) -> Path:
        self.output_dir = Path(output_dir) / "exchange_logging"
        self.output_dir.mkdir(parents=True, exist_ok=True)

        exchanges_dicts = {}
        for exchange in self.exchanges:
            exchanges_dicts[exchange] = {"type": "netcdf"}
        output_config = {
            "general": {"output_dir": str(self.output_dir)},
            "exchanges": exchanges_dicts,
        }
        output_config_toml_path = self.output_dir / "logging.toml"
        with open(output_config_toml_path, "wb") as f:
            tomli_w.dump(output_config, f)

        return output_config_toml_path

    def get_results(
        self,
    ) -> xr.Dataset[Any]:
        results = {}
        for exchange in self.exchanges:
            nc_file = self.output_dir / (exchange + ".nc")
            results[exchange] = xr.open_dataarray(nc_file)
        return results


class Results(NamedTuple):
    basin_df: pd.DataFrame
    flow_df: pd.DataFrame
    allocation_df: pd.DataFrame
    mf6head: xr.DataArray
    mf6_budgets: dict[str, xr.DataArray]
    msw_budgets: xr.Dataset[Any]
    exchange_budget: dict[Any]


def get_coupled_mf6_package_mask(
    coupling_file: Path, package_mask: xr.DataArray
) -> xr.DataArray:
    coupling_data = pd.read_csv(coupling_file, delimiter="\t")
    basin_indices = coupling_data["basin_index"].to_numpy()
    mask_ar = np.copy(package_mask.to_numpy())
    mask_ar[~np.isnan(mask_ar)] = basin_indices
    return xr.DataArray(
        data=mask_ar.reshape(
            package_mask.layer.size, package_mask.y.size, package_mask.x.size
        ),
        coords=package_mask.coords,
        dims=package_mask.dims,
    ).dropna(dim="layer", how="all")


def get_coupled_msw_mask(
    coupling_file: Path, mf6_idomain: xr.DataArray
) -> xr.DataArray:
    coupling_data = pd.read_csv(coupling_file, delimiter="\t")
    svat_indices = coupling_data["svat_index"].to_numpy() - 1
    basin_indices = coupling_data["basin_index"].to_numpy()
    node2svat = pd.read_csv(
        coupling_file.parent / "nodenr2svat.dxc", delimiter="\s+", header=None
    )
    mf6_user_indices = np.arange(mf6_idomain.size)[
        mf6_idomain.to_numpy().ravel() == 1
    ]  # indices based on total model domain (active and inactive)
    mf6_model_indices = (
        node2svat[0].to_numpy() - 1
    )  # indices based on active model domain (idomain > 0)
    svats = np.zeros(mf6_model_indices.size)
    svats[svat_indices] = 1
    coupled = np.flatnonzero(svats)

    mask_ar = np.full((mf6_idomain.size), fill_value=np.nan)  # user shape model domain
    # take coupled selection of model indices and translate to user indices
    mask_ar[mf6_user_indices[mf6_model_indices[coupled]]] = (
        basin_indices + 1
    )  # one based
    return xr.DataArray(
        data=mask_ar.reshape(
            mf6_idomain.layer.size, mf6_idomain.y.size, mf6_idomain.x.size
        ),
        coords=mf6_idomain.coords,
        dims=mf6_idomain.dims,
    ).dropna(dim="layer", how="all")


def get_metaswap_results(
    workdir: Path,
    coupling_file: Path,
    mf6_idomain: xr.DataArray,
    vars: list[str] = ["bdgqrun"],
) -> xr.Dataset:
    mask = get_coupled_msw_mask(coupling_file, mf6_idomain)
    out = {}
    for var in vars:
        ar = imod.idf.open(Path(workdir) / var / "bdgqrun_*_L1.IDF")
        ar["x"] = np.round(ar.x.values, decimals=1)
        ar["y"] = np.round(ar.y.values, decimals=1)
        out[var] = ar.where(mask.notnull())
    out["index_mask"] = mask
    return xr.Dataset(out)


def flatten(array: xr.DataArray) -> np.ndarray:
    out = array.stack(z=["layer", "y", "x"]).to_numpy()
    return out[~np.isnan(out)]


def sum_budgets(to_sum: np.ndarray, summed: np.ndarray) -> np.ndarray:
    if summed.size == 0:
        summed = to_sum
    else:
        summed += to_sum
    return summed


def assert_results(
    tmp_path_dev: Path,
    ribametamod_model: RibaMetaMod,
    results: Results,
    atol: float = 0.5,
) -> None:
    # get n-basins
    n_basins = results.basin_df["node_id"].unique()
    basin_index = -1
    for n_basin in n_basins:
        basin_index += 1
        basin_mask = results.basin_df["node_id"] == n_basin
        svat_mask = results.msw_budgets["index_mask"] == basin_index
        delt = 24 * 60 * 60

        # Ribasim results
        ribasim_bnd_flux = (
            results.basin_df["drainage"] - results.basin_df["infiltration"]
        ).to_numpy()[basin_mask] * delt

        # MetaSWAP runoff
        area = results.msw_budgets["bdgqrun"].dx * -results.msw_budgets["bdgqrun"].dy
        runoff_msw = (
            (-results.msw_budgets["bdgqrun"] * area)
            .where(svat_mask)
            .sum(dim=["y", "x", "layer"], skipna=True)
            .to_numpy()
        )
        runoff_exchange = (
            results.exchange_budget["exchange_demand_sw_ponding"].to_numpy()
        )[:, basin_index] * delt  # [basin_mask.ravel()]
        plot_results(
            tmp_path_dev,
            {
                "runoff_msw": runoff_msw,
                "runoff exchange_dashed": runoff_exchange,
            },
            "metaswap_results_basin_" + str(n_basin),
        )
        # np.testing.assert_allclose(
        #     runoff_msw,
        #     runoff_exchange,
        #     atol=atol,
        # )  # MetaSWAP output relative to coupler

        # River fluxes; summed per coupled basin
        mf6_model = ribametamod_model.mf6_simulation["GWF_1"]
        summed_riv_flux_estimate = np.array([])
        summed_correction_flux = np.array([])
        for item in ribametamod_model.coupling_list:
            if isinstance(item, RibaModActiveDriverCoupling):
                for package in item.mf6_packages:
                    # river flux estimate from coupler logging
                    coupling_file = tmp_path_dev / "exchanges" / (package + ".tsv")
                    basin_indices = pd.read_csv(coupling_file, delimiter="\t")[
                        "basin_index"
                    ].to_numpy()
                    riv_flux_estimate_exchange = (
                        results.exchange_budget["exchange_demand_" + package].to_numpy()
                    )[:, basin_index] * delt
                    summed_riv_flux_estimate = sum_budgets(
                        riv_flux_estimate_exchange, summed_riv_flux_estimate
                    )

                    # compute river flux as expected in mf6 output, based on the set stages via coupler
                    cond_ar = mf6_model[package].dataset["conductance"]
                    package_basin_mask = (
                        get_coupled_mf6_package_mask(coupling_file, cond_ar)
                        == basin_index
                    )
                    nriv = int(cond_ar.notnull().where(package_basin_mask).sum().item())
                    ntime = results.mf6head.time.size

                    subset_head = flatten(
                        results.mf6head.where(cond_ar.notnull() & package_basin_mask)
                    ).reshape(ntime, nriv)

                    stage = results.exchange_budget["stage_" + package].to_numpy()[
                        :, basin_indices == basin_index
                    ]
                    bottom = flatten(
                        mf6_model[package]
                        .dataset["bottom_elevation"]
                        .where(package_basin_mask)
                    )
                    cond = flatten(cond_ar.where(package_basin_mask))
                    riv_flux_exchange = (
                        (stage - np.maximum(subset_head, bottom)) * cond
                    ).sum(axis=1)
                    # river flux from MF6 output
                    riv_flux_output = (
                        flatten(results.mf6_budgets[package].where(package_basin_mask))
                        .reshape(ntime, nriv)
                        .sum(axis=1)
                    )
                    # riv correction flux from MF6 output
                    riv_correction_flux = (
                        flatten(
                            results.mf6_budgets["api_" + package].where(
                                package_basin_mask
                            )
                        )
                        .reshape(ntime, nriv)
                        .sum(axis=1)
                    )
                    summed_correction_flux = sum_budgets(
                        riv_correction_flux, summed_correction_flux
                    )
                    plot_results(
                        tmp_path_dev,
                        {
                            "river flux estimate": -riv_flux_estimate_exchange,
                            "river flux MF6-output": riv_flux_output,
                            "river flux correction MF6-output": riv_correction_flux,
                            "river flux validation_dashed": riv_flux_exchange,
                        },
                        package + "_basin_" + str(n_basin),
                    )
                    # np.testing.assert_allclose(
                    #     riv_flux_exchange, riv_flux_output, atol=atol
                    # )
            elif isinstance(item, RibaModPassiveDriverCoupling):
                for package in item.mf6_packages:
                    # river flux estimate from coupler logging
                    riv_flux_estimate_exchange = (
                        results.exchange_budget["exchange_demand_" + package].to_numpy()
                    )[:, 0][basin_mask.ravel()] * delt
                    summed_riv_flux_estimate = sum_budgets(
                        riv_flux_estimate_exchange, summed_riv_flux_estimate
                    )
        # evaluate total coupled waterbalance
        plot_results(
            tmp_path_dev,
            {
                "MetaSWAP runoff": runoff_exchange,
                "MF6 river flux estimate": summed_riv_flux_estimate,
                "MF6 river flux correction": summed_correction_flux,
                "Ribasim": ribasim_bnd_flux,
                "sum exchanges_dashed": runoff_exchange
                + summed_riv_flux_estimate
                + summed_correction_flux,
            },
            "results",
        )
        # np.testing.assert_allclose(
        #     ribasim_bnd_flux,
        #     runoff_exchange + summed_riv_flux_estimate + summed_correction_flux,
        #     atol=atol,
        # )


def plot_results(tmp_path_dev: Path, results: dict[str, np.ndarray], name: str) -> None:
    for label, result in results.items():
        if "dashed" in label:
            plt.plot(result, label=label.replace("_dashed", ""), linestyle="dashed")
        else:
            plt.plot(result, label=label)
    plt.legend()
    plt.title(name)
    plt.savefig(tmp_path_dev / (name + ".png"))
    plt.clf()


def write_run_read(
    tmp_path: Path,
    ribametamod_model: RibaMetaMod,
    modflow_dll: Path,
    ribasim_dll: Path,
    ribasim_dll_dep_dir: Path,
    metaswap_dll: Path,
    metaswap_dll_dep_dir: Path,
    run_coupler_function: Callable[[Path], None],
    output_labels: list[str] | None = None,
) -> Results:
    """
    Write the model, run it, read and return the results.
    """
    if output_labels is not None:
        exchange_budgets = exchange_output(output_labels)
        output_config_file = exchange_budgets.write_toml(
            tmp_path,
        )

    ribametamod_model.write(
        tmp_path,
        modflow6_dll=modflow_dll,
        ribasim_dll=ribasim_dll,
        ribasim_dll_dependency=ribasim_dll_dep_dir,
        metaswap_dll=metaswap_dll,
        metaswap_dll_dependency=metaswap_dll_dep_dir,
        modflow6_write_kwargs={"binary": False},
        output_config_file=output_config_file,
    )

    run_coupler_function(tmp_path / ribametamod_model._toml_name)

    # get exchange budgets
    exchange_budget = exchange_budgets.get_results()

    # Read Ribasim output
    basin_df = pd.read_feather(
        tmp_path / ribametamod_model._ribasim_model_dir / "results" / "basin.arrow"
    )
    flow_df = pd.read_feather(
        tmp_path / ribametamod_model._ribasim_model_dir / "results" / "flow.arrow"
    )
    allocation_df = pd.read_feather(
        tmp_path / ribametamod_model._ribasim_model_dir / "results" / "allocation.arrow"
    )

    # Read MODFLOW 6 output
    head = imod.mf6.open_hds(
        tmp_path / ribametamod_model._modflow6_model_dir / "GWF_1" / "GWF_1.hds",
        tmp_path / ribametamod_model._modflow6_model_dir / "GWF_1" / "dis.dis.grb",
    ).compute()

    budgets = imod.mf6.open_cbc(
        tmp_path / ribametamod_model._modflow6_model_dir / "GWF_1" / "GWF_1.cbc",
        tmp_path / ribametamod_model._modflow6_model_dir / "GWF_1" / "dis.dis.grb",
        simulation_start_time=pd.to_datetime("2000/01/01"),
        time_unit="d",
    )
    # get MetaSWAP results from idf output
    mf6_idomain = ribametamod_model.mf6_simulation["GWF_1"]["dis"]["idomain"]
    msw_results = get_metaswap_results(
        ribametamod_model._metaswap_model_dir,
        tmp_path / "exchanges" / "msw_ponding.tsv",
        mf6_idomain,
    )

    return Results(
        basin_df, flow_df, allocation_df, head, budgets, msw_results, exchange_budget
    )


# @pytest.mark.xdist_group(name="ribasim")
@pytest.mark.skip("potentially hangs, to be solved in model")
@parametrize_with_cases("ribametamod_model", glob="backwater_model")
def test_ribametamod_backwater(
    tmp_path_dev: Path,
    ribametamod_model: RibaMetaMod | MetaSwapModel,
    modflow_dll_devel: Path,
    ribasim_dll_devel: Path,
    ribasim_dll_dep_dir_devel: Path,
    metaswap_dll_devel: Path,
    metaswap_dll_dep_dir_devel: Path,
    run_coupler_function: Callable[[Path], None],
) -> None:
    """
    Test if the backwater model works as expected
    """
    results = write_run_read(
        tmp_path_dev,
        ribametamod_model,
        modflow_dll_devel,
        ribasim_dll_devel,
        ribasim_dll_dep_dir_devel,
        metaswap_dll_devel,
        metaswap_dll_dep_dir_devel,
        run_coupler_function,
    )
    assert_results(tmp_path_dev, ribametamod_model, results)


@pytest.mark.xdist_group(name="ribasim")
@parametrize_with_cases("ribametamod_model", glob="bucket_model")
# @pytest.mark.skip("potentially hangs, to be solved in model")
def test_ribametamod_bucket(
    tmp_path_dev: Path,
    ribametamod_model: RibaMetaMod,
    modflow_dll_devel: Path,
    ribasim_dll_devel: Path,
    ribasim_dll_dep_dir_devel: Path,
    metaswap_dll_devel: Path,
    metaswap_dll_dep_dir_devel: Path,
    run_coupler_function: Callable[[Path], None],
) -> None:
    """
    Test if the bucket model works as expected
    """
    results = write_run_read(
        tmp_path_dev,
        ribametamod_model,
        modflow_dll_devel,
        ribasim_dll_devel,
        ribasim_dll_dep_dir_devel,
        metaswap_dll_devel,
        metaswap_dll_dep_dir_devel,
        run_coupler_function,
        output_labels=[
            "exchange_demand_riv-1",
            "exchange_demand_sw_ponding",
            "stage_riv-1",
        ],
    )
    assert_results(tmp_path_dev, ribametamod_model, results)


# @pytest.mark.xdist_group(name="ribasim")
@parametrize_with_cases("ribametamod_model", glob="two_basin_model")
def test_ribametamod_two_basin(
    tmp_path_dev: Path,
    ribametamod_model: RibaMetaMod,
    metaswap_dll_devel: Path,
    metaswap_dll_dep_dir_devel: Path,
    modflow_dll_devel: Path,
    ribasim_dll_devel: Path,
    ribasim_dll_dep_dir_devel: Path,
    run_coupler_function: Callable[[Path], None],
) -> None:
    """
    Test if the two-basin model model works as expected
    """
    results = write_run_read(
        tmp_path_dev,
        ribametamod_model,
        modflow_dll_devel,
        ribasim_dll_devel,
        ribasim_dll_dep_dir_devel,
        metaswap_dll_devel,
        metaswap_dll_dep_dir_devel,
        run_coupler_function,
        output_labels=[
            "exchange_demand_riv_1",
            "exchange_demand_sw_ponding",
            "stage_riv_1",
        ],
    )
    assert_results(tmp_path_dev, ribametamod_model, results)


# @pytest.mark.xdist_group(name="ribasim")
@pytest.mark.skip("potentially hangs, to be solved in model")
@parametrize_with_cases("ribametamod_model", glob="two_basin_model_users")
def test_ribametamod_two_basin_users(
    tmp_path_dev: Path,
    #   ribametamod_model_users: RibaMetaMod,
    ribametamod_model: RibaMetaMod,
    metaswap_dll_devel: Path,
    metaswap_dll_dep_dir_devel: Path,
    modflow_dll_devel: Path,
    ribasim_dll_devel: Path,
    ribasim_dll_dep_dir_devel: Path,
    run_coupler_function: Callable[[Path], None],
    ribametamod_two_basin_tot_svat_ref: Path,
) -> None:
    """
    Test if the two-basin model model works with two water users (sprinkling)
    """
    ribametamod_model.write(
        tmp_path_dev,
        modflow6_dll=modflow_dll_devel,
        ribasim_dll=ribasim_dll_devel,
        ribasim_dll_dependency=ribasim_dll_dep_dir_devel,
        metaswap_dll=metaswap_dll_devel,
        metaswap_dll_dependency=metaswap_dll_dep_dir_devel,
    )

    run_coupler_function(tmp_path_dev / ribametamod_model._toml_name)


def test_exchange_balance() -> None:
    shape = 4
    labels = ["flux-1", "flux-2"]
    exchange = ExchangeBalance(shape=shape, labels=labels)

    # exchange demands to class
    array_negative = np.zeros(shape=shape, dtype=np.float64)
    array_positive = np.zeros(shape=shape, dtype=np.float64)

    # seperate negative contributions for n:1 exchange
    array_negative[0] = -10
    array_negative[1] = -10
    array_positive[0] = 0.0
    array_positive[1] = 5.0

    demand_array = array_negative + array_positive
    exchange.demands["flux-1"] = demand_array
    exchange.demands["flux-2"] = demand_array * 0.5
    exchange.demands_negative["flux-1"] = array_negative
    exchange.demands_negative["flux-2"] = array_negative * 0.5

    # check summed demand
    assert np.all(exchange.demand == demand_array + (demand_array * 0.5))
    # check summed negative demand
    assert np.all(exchange.demand_negative == array_negative + (array_negative * 0.5))

    # evaluate realised method
    realised = np.zeros(shape=shape, dtype=np.float64)
    realised[0] = -5.0
    realised[1] = -5.0
    # compute
    exchange.compute_realised(realised)
    # compare: realised_factor = 1 - (-shortage - sum_negative_demands)
    realised_factor = np.zeros(shape=shape, dtype=np.float64)
    realised_factor[0] = 1 - (-10 / -15)
    realised_factor[1] = 1 - (-2.5 / -15)

    expected_flux1 = np.zeros(shape=shape, dtype=np.float64)
    expected_flux2 = np.zeros(shape=shape, dtype=np.float64)
    expected_flux1[0] = realised_factor[0] * array_negative[0]
    expected_flux2[0] = realised_factor[0] * array_negative[0] * 0.5
    expected_flux1[1] = realised_factor[1] * array_negative[1]
    expected_flux2[1] = realised_factor[1] * array_negative[1] * 0.5
    assert np.all(expected_flux1 == exchange.realised_negative["flux-1"])
    assert np.all(expected_flux2 == exchange.realised_negative["flux-2"])

    compute_realised = np.zeros(shape=shape, dtype=np.float64)
    compute_realised[0] = (
        exchange.realised_negative["flux-1"][0]
        + exchange.realised_negative["flux-2"][0]
        + array_positive[0]
        + (array_positive[0] * 0.5)
    )
    compute_realised[1] = (
        exchange.realised_negative["flux-1"][1]
        + exchange.realised_negative["flux-2"][1]
        + array_positive[1]
        + (array_positive[1] * 0.5)
    )
    assert np.all(np.isclose(realised, compute_realised))

    # check if reset zeros arrays
    exchange.reset()
    assert np.all(exchange.demand == np.zeros(shape=shape, dtype=np.float64))
    assert np.all(exchange.demand_negative == np.zeros(shape=shape, dtype=np.float64))

    # check if errors are thrown
    # shortage larger than negative demands
    shape = 1
    labels = ["flux-1"]
    exchange = ExchangeBalance(shape=shape, labels=labels)
    exchange.demands["flux-1"] = np.ones(shape=shape, dtype=np.float64) * -4
    exchange.demands_negative["flux-1"] = np.ones(shape=shape, dtype=np.float64) * -4
    realised = np.ones(shape=shape, dtype=np.float64) * 2
    with pytest.raises(
        ValueError,
        match="Invalid realised values: found shortage larger than negative demand contributions",
    ):
        exchange.compute_realised(realised)

    # shortage for positive demand
    shape = 1
    labels = ["flux-1"]
    exchange = ExchangeBalance(shape=shape, labels=labels)
    exchange.demands["flux-1"] = np.ones(shape=shape, dtype=np.float64) * 10
    exchange.demands_negative["flux-1"] = np.ones(shape=shape, dtype=np.float64) * -4
    realised = np.ones(shape=shape, dtype=np.float64) * 8
    with pytest.raises(
        ValueError, match="Invalid realised values: found shortage for positive demand"
    ):
        exchange.compute_realised(realised)
