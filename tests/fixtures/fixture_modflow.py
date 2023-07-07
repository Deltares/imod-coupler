import numpy as np
import pandas as pd
import pytest_cases
import xarray as xr
from imod import mf6

from .common import create_wells, get_times, grid_sizes


def make_mf6_model(idomain: xr.DataArray) -> mf6.GroundwaterFlowModel:
    _, _, layer, _, _, dz = grid_sizes()
    nlay = len(layer)

    top = 0.0
    bottom = top - xr.DataArray(np.cumsum(dz), coords={"layer": layer}, dims="layer")

    gwf_model = mf6.GroundwaterFlowModel()
    gwf_model["dis"] = mf6.StructuredDiscretization(
        idomain=idomain, top=top, bottom=bottom
    )

    icelltype = xr.full_like(bottom, 0, dtype=int)
    k_values = np.ones((nlay))
    k_values[1, ...] = 0.01

    k = xr.DataArray(k_values, {"layer": layer}, ("layer",))
    k33 = xr.DataArray(k_values / 10.0, {"layer": layer}, ("layer",))
    gwf_model["npf"] = mf6.NodePropertyFlow(
        icelltype=icelltype,
        k=k,
        k33=k33,
        variable_vertical_conductance=True,
        dewatered=False,
        perched=False,
        save_flows=True,
    )

    gwf_model["ic"] = mf6.InitialConditions(start=-2.0)
    gwf_model["sto"] = mf6.SpecificStorage(1e-3, 0.1, True, 0)
    gwf_model["oc"] = mf6.OutputControl(save_head="last", save_budget="last")
    return gwf_model


def make_mf6_simulation(gwf_model: mf6.GroundwaterFlowModel) -> mf6.Modflow6Simulation:
    times = get_times()
    simulation = mf6.Modflow6Simulation("test")
    simulation["GWF_1"] = gwf_model
    simulation["solver"] = mf6.Solution(
        modelnames=["GWF_1"],
        print_option="summary",
        csv_output=False,
        no_ptc=True,
        outer_dvclose=1.0e-4,
        outer_maximum=500,
        under_relaxation=None,
        inner_dvclose=1.0e-4,
        inner_rclose=0.001,
        inner_maximum=100,
        linear_acceleration="cg",
        scaling_method=None,
        reordering_method=None,
        relaxation_factor=0.97,
    )
    simulation.create_time_discretization(additional_times=times)
    return simulation


def make_coupled_mf6_model(idomain: xr.DataArray) -> mf6.Modflow6Simulation:
    _, nrow, ncol = idomain.shape
    gwf_model = make_mf6_model(idomain)
    times = get_times()
    head = xr.full_like(idomain.astype(np.float64), np.nan)
    head[0, :, 0] = -2.0
    head = head.expand_dims(time=times)
    gwf_model["chd"] = mf6.ConstantHead(
        head, print_input=True, print_flows=True, save_flows=True
    )

    recharge = xr.zeros_like(idomain.sel(layer=1), dtype=float)
    recharge[:, 0] = np.nan
    recharge = recharge.where(idomain.sel(layer=1))

    gwf_model["rch_msw"] = mf6.Recharge(recharge)
    gwf_model["wells_msw"] = create_wells(nrow, ncol, idomain)

    simulation = make_mf6_simulation(gwf_model)
    return simulation


def make_coupled_ribasim_mf6_model(idomain: xr.DataArray) -> mf6.Modflow6Simulation:
    # The bottom of the ribasim trivial model is located at 0.0 m: the surface
    # level of the groundwater model.
    gwf_model = make_mf6_model(idomain)

    template = xr.full_like(idomain.isel(layer=[0]), np.nan, dtype=np.float64)
    stage = template.copy()
    conductance = template.copy()
    bottom_elevation = template.copy()

    # Conductance is area divided by resistance (dx * dy / c0)
    # Assume the entire cell is wetted.
    stage[:, 1, 3] = 0.5
    conductance[:, 1, 3] = (100.0 * 100.0) / 1.0
    bottom_elevation[:, 1, 3] = 0.0

    gwf_model["riv-1"] = mf6.River(
        stage=stage,
        conductance=conductance,
        bottom_elevation=bottom_elevation,
        save_flows=True,
    )

    simulation = make_mf6_simulation(gwf_model)
    return simulation


def convert_storage_package(
    gwf_model: mf6.GroundwaterFlowModel,
) -> mf6.GroundwaterFlowModel:
    """
    Convert existing groundwater flow model with a specific storage to a model
    with a storage coefficient.
    """
    # Specific storage package
    sto_ds = gwf_model.pop("sto").dataset

    # Confined: S = Ss * b
    # Where 'S' is storage coefficient, 'Ss' specific
    # storage, and 'b' thickness.
    # https://en.wikipedia.org/wiki/Specific_storage

    dis_ds = gwf_model["dis"].dataset
    top = dis_ds["bottom"].shift(layer=1)
    top[0] = dis_ds["top"]
    b = top - dis_ds["bottom"]

    sto_ds["storage_coefficient"] = sto_ds["specific_storage"] * b
    sto_ds = sto_ds.drop_vars("specific_storage")

    gwf_model["sto"] = mf6.StorageCoefficient(**sto_ds)
    return gwf_model


def make_idomain() -> xr.DataArray:
    x, y, layer, dx, dy, _ = grid_sizes()

    nlay = len(layer)
    nrow = len(y)
    ncol = len(x)

    return xr.DataArray(
        data=np.ones((nlay, nrow, ncol), dtype=np.int32),
        dims=("layer", "y", "x"),
        coords={"layer": layer, "y": y, "x": x, "dx": dx, "dy": dy},
    )


@pytest_cases.fixture(scope="function")
def active_idomain() -> xr.DataArray:
    """Return all active idomain"""
    idomain = make_idomain()

    return idomain


@pytest_cases.fixture(scope="function")
def inactive_idomain() -> xr.DataArray:
    """Return idomain with an inactive cell"""
    idomain = make_idomain()
    # Deactivate middle cell
    idomain[:, 1, 2] = 0

    return idomain


@pytest_cases.fixture(scope="function")
def coupled_mf6_model(active_idomain: xr.DataArray) -> mf6.Modflow6Simulation:
    return make_coupled_mf6_model(active_idomain)


@pytest_cases.fixture(scope="function")
def coupled_ribasim_mf6_model(active_idomain: xr.DataArray) -> mf6.Modflow6Simulation:
    return make_coupled_ribasim_mf6_model(active_idomain)


@pytest_cases.fixture(scope="function")
def coupled_mf6_model_storage_coefficient(
    active_idomain: xr.DataArray,
) -> mf6.Modflow6Simulation:
    coupled_mf6_model = make_coupled_mf6_model(active_idomain)

    gwf_model = coupled_mf6_model["GWF_1"]
    gwf_model = convert_storage_package(gwf_model)
    # reassign gwf model
    coupled_mf6_model["GWF_1"] = gwf_model

    return coupled_mf6_model


@pytest_cases.fixture(scope="function")
def coupled_mf6_model_inactive(
    inactive_idomain: xr.DataArray,
) -> mf6.Modflow6Simulation:
    """coupled mf6 model with an inactive cell"""
    return make_coupled_mf6_model(inactive_idomain)


@pytest_cases.fixture(scope="function")
def mf6_model_with_river(
    coupled_mf6_model: mf6.Modflow6Simulation,
) -> mf6.Modflow6Simulation:
    flow_model = coupled_mf6_model["GWF_1"]
    idomain = flow_model["dis"].dataset["idomain"]
    stage = xr.full_like(idomain.sel({"layer": 1}), dtype=np.floating, fill_value=3.1)
    conductance = xr.full_like(stage, 4.2)
    bottom_elevation = xr.full_like(stage, 0.3)
    bottom_elevation[{"x": 2}] = -0.1
    river_package = mf6.River(stage, conductance, bottom_elevation, save_flows=True)
    flow_model["Oosterschelde"] = river_package
    return coupled_mf6_model


@pytest_cases.fixture(scope="function")
def mf6_model_backwater_river() -> mf6.Modflow6Simulation:
    """
    This model is created to match the Ribasim backwater test model.
    """
    x = np.arange(10.0, 1020.0, 20.0)
    y = np.arange(200.0, -220.0, -20.0)
    layer = np.array([1])
    shape = (layer.size, y.size, x.size)
    dims = ["layer", "y", "x"]
    coords = {"layer": layer, "y": y, "x": x}
    idomain = xr.DataArray(data=np.ones(shape, dtype=int), coords=coords, dims=dims)

    gwf_model = mf6.GroundwaterFlowModel()
    gwf_model["dis"] = mf6.StructuredDiscretization(
        idomain=idomain, top=20.0, bottom=xr.DataArray([-10.0], dims=["layer"])
    )

    gwf_model["npf"] = mf6.NodePropertyFlow(
        icelltype=0,
        k=0.1,
        k33=0.1,
    )

    gwf_model["rch"] = mf6.Recharge(rate=xr.full_like(idomain, 0.001, dtype=float))

    stage = xr.full_like(idomain, np.nan, dtype=float)
    conductance = xr.full_like(idomain, np.nan, dtype=float)
    bottom_elevation = xr.full_like(idomain, np.nan, dtype=float)
    stage[:, 10, :] = 0.5
    # Compute conductance as wetted area (length 20.0, width 1.0, entry resistance 1.0)
    conductance[:, 10, :] = (20.0 * 1.0) / 1.0
    bottom_elevation[:, 10, :] = 0.0
    gwf_model["riv-1"] = mf6.River(
        stage=stage,
        conductance=conductance,
        bottom_elevation=bottom_elevation,
        save_flows=True,
    )
    gwf_model["drn-1"] = mf6.Drainage(
        elevation=stage,
        conductance=conductance,
    )

    gwf_model["ic"] = mf6.InitialConditions(start=0.0)
    gwf_model["sto"] = mf6.SpecificStorage(1e-3, 0.3, True, 1)
    gwf_model["oc"] = mf6.OutputControl(save_head="last", save_budget="last")

    simulation = mf6.Modflow6Simulation("backwater")
    simulation["GWF_1"] = gwf_model
    simulation["solver"] = mf6.Solution(
        modelnames=["GWF_1"],
        print_option="summary",
        csv_output=False,
        no_ptc=True,
        outer_dvclose=1.0e-4,
        outer_maximum=500,
        under_relaxation=None,
        inner_dvclose=1.0e-4,
        inner_rclose=0.001,
        inner_maximum=100,
        linear_acceleration="cg",
        scaling_method=None,
        reordering_method=None,
        relaxation_factor=0.97,
    )
    times = pd.date_range("2020-01-01", "2030-01-01", freq="M")
    simulation.create_time_discretization(additional_times=times)
    return simulation
