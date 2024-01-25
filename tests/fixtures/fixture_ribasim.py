import geopandas as gpd
import numpy as np
import pandas as pd
import pytest_cases
import ribasim
import ribasim_testmodels


def add_subgrid(model: ribasim.Model) -> ribasim.Model:
    """Add 1:1 subgrid levels to model"""

    profile_df = model.basin.profile.df
    _, basin_id = np.unique(profile_df["node_id"], return_inverse=True)
    geometry = model.network.node.df.loc[profile_df["node_id"]].geometry
    subgrid_df = pd.DataFrame(
        data={
            "node_id": profile_df["node_id"],
            "basin_id": basin_id,
            "subgrid_id": basin_id,
            "basin_level": profile_df["level"],
            "subgrid_level": profile_df["level"],
            "meta_x": geometry.x.to_numpy(),
            "meta_y": geometry.y.to_numpy(),
        }
    )
    model.basin.subgrid.df = subgrid_df
    # TODO: this is currently required because the ribasim-api doesn't have a
    # update_subgrid_level function yet. Once implemented, this can be removed
    # and the function should be called in the ribamod update() call.
    model.results.subgrid = True
    model.solver.saveat = 86400.0  # always daily
    return model


@pytest_cases.fixture(scope="function")
def ribasim_bucket_model() -> ribasim.Model:
    return add_subgrid(ribasim_testmodels.bucket_model())


@pytest_cases.fixture(scope="function")
def ribasim_backwater_model() -> ribasim.Model:
    return add_subgrid(ribasim_testmodels.backwater_model())


@pytest_cases.fixture(scope="function")
def ribasim_two_basin_model() -> ribasim.Model:
    flow_boundary = ribasim.FlowBoundary(
        static=pd.DataFrame(
            data={
                "node_id": [1],
                "flow_rate": [1e-2],
            }
        )
    )

    xy = np.array(
        [
            (0, 0.0),  # FlowBoundary
            (250.0, 0.0),  # Basin 1
            (750.0, 0.0),  # Basin 2
            (1000.00, 0.0),  # TabulatedRatingCurve
            (1100.00, 0.0),  # Terminal
        ]
    )
    # Rectangular profile
    profile = pd.DataFrame(
        data={
            "node_id": [2, 2, 3, 3],
            "area": [400.0, 400.0, 400.0, 400.0],
            "level": [0.0, 1.0, 0.0, 1.0],
        }
    )
    state = pd.DataFrame(data={"node_id": [2, 3], "level": [0.01, 0.01]})
    static = pd.DataFrame(
        data={
            "node_id": [2, 3],
            "drainage": [0.0, 0.0],
            "potential_evaporation": [0.0, 0.0],
            "infiltration": [0.0, 0.0],
            "precipitation": [0.0, 0.0],
            "urban_runoff": [0.0, 0.0],
        }
    )
    subgrid = pd.DataFrame(
        data={
            "node_id": [2, 2, 3, 3],
            "subgrid_id": [1, 1, 2, 2],
            "basin_level": [0.0, 1.0, 0.0, 1.0],
            "subgrid_level": [0.0, 1.0, 0.0, 1.0],
            "meta_x": [250.0, 250.0, 750.0, 750.0],
            "meta_y": [0.0, 0.0, 0.0, 0.0],
        }
    )
    basin = ribasim.Basin(profile=profile, state=state, static=static, subgrid=subgrid)

    rating_curve = ribasim.TabulatedRatingCurve(
        static=pd.DataFrame(
            data={
                "node_id": [4, 4],
                "level": [0.0, 1.0],
                "discharge": [0.0, 0.01],
            }
        )
    )

    terminal = ribasim.Terminal(
        static=pd.DataFrame(
            data={
                "node_id": [5],
            }
        )
    )
    node_id, node_type = ribasim.Node.node_ids_and_types(
        basin,
        rating_curve,
        flow_boundary,
        terminal,
    )
    node_xy = gpd.points_from_xy(x=xy[:, 0], y=xy[:, 1])

    # Make sure the feature id starts at 1: explicitly give an index.
    node = ribasim.Node(
        df=gpd.GeoDataFrame(
            data={"type": node_type},
            index=pd.Index(node_id, name="fid"),
            geometry=node_xy,
            crs="EPSG:28992",
        )
    )

    from_id = np.array([1, 3, 4], dtype=np.int64)
    to_id = np.array([2, 4, 5], dtype=np.int64)
    lines = node.geometry_from_connectivity(from_id, to_id)
    edge = ribasim.Edge(
        df=gpd.GeoDataFrame(
            data={
                "from_node_id": from_id,
                "to_node_id": to_id,
                "edge_type": len(from_id) * ["flow"],
            },
            geometry=lines,
            crs="EPSG:28992",
        )
    )

    ribasim_model = ribasim.Model(
        network=ribasim.Network(node=node, edge=edge),
        basin=basin,
        flow_boundary=flow_boundary,
        tabulated_rating_curve=rating_curve,
        terminal=terminal,
        starttime="2020-01-01 00:00:00",
        endtime="2030-01-01 00:00:00",
    )
    return ribasim_model
