from typing import Optional

import numpy as np
from numpy.typing import NDArray
from xmipy import XmiWrapper


class MswWrapper(XmiWrapper):
    def get_surfacewater_sprinking_demand(self) -> NDArray[np.float_]:
        """returns the sprinkling volume demand from metaswap

        Parameters
        ----------
        none

        Returns
        -------
         NDArray[np.float_]:
            sprinkling demand of MetaSWAP in m3/dtgw. Array as a copy of the MetaSWAP intenal array,
            since the set function uses a different bmi/xmi-variable
        """
        return self.get_value("dts2dfmputsp")

    def set_surfacewater_sprinking_demand(
        self, sprinking_demand: NDArray[np.float_]
    ) -> None:
        """sets the sprinkling volume demand in metaswap

        Parameters
        ----------
        sprinkiling_demand: NDArray[np.float_]:
            sprinkling demand of MetaSWAP in m3/dtgw

        Returns
        -------
        none

        """
        self.set_value("dts2dfmgetsp", sprinking_demand)

    def get_surfacewater_ponding_allocation(self) -> NDArray[np.float_]:
        """returns the ponding volume allocation from metaswap

        Parameters
        ----------
        none

        Returns
        -------
         NDArray[np.float_]:
            ponding volume allocation of MetaSWAP in m3/dtsw. Array as a copy of the MetaSWAP intenal array,
            since the set function uses a different bmi/xmi-variable
        """
        return self.get_value("ts2dfmput")

    def set_surfacewater_ponding_allocation(
        self, ponding_allocation: NDArray[np.float_]
    ) -> None:
        """sets ponding volume allocation in metaswap

        Parameters
        ----------
        ponding_allocation: NDArray[np.float_]
            ponding volume allocation to set in metaswap in m3/dtsw

        Returns
        -------
        none
        """
        self.set_value("ts2dfmget", ponding_allocation)

    def set_ponding_level_2d(self, ponding_level_2d: NDArray[np.float_]) -> None:
        """sets ponding level from dlfow-2d in metaswap

        Parameters
        ----------
        ponding_level_2d: NDArray[np.float_]
            ponding level to set in metaswap in m relative to msw soil surface elevation (depth)

        Returns
        -------
        none
        """
        self.set_value("dfm2lvswk", ponding_level_2d)
