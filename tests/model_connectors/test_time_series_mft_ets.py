# :copyright (c) URBANopt, Alliance for Sustainable Energy, LLC, and other contributors.
# See also https://github.com/urbanopt/geojson-modelica-translator/blob/develop/LICENSE.md

import os
import re
from pathlib import Path

import pytest
from buildingspy.io.outputfile import Reader
from modelica_builder.model import Model

from geojson_modelica_translator.geojson.urbanopt_geojson import (
    UrbanOptGeoJson
)
from geojson_modelica_translator.model_connectors.couplings.coupling import (
    Coupling
)
from geojson_modelica_translator.model_connectors.couplings.graph import (
    CouplingGraph
)
from geojson_modelica_translator.model_connectors.districts.district import (
    District
)
from geojson_modelica_translator.model_connectors.energy_transfer_systems import (
    CoolingIndirect,
    HeatingIndirect
)
from geojson_modelica_translator.model_connectors.load_connectors.time_series_mft_ets_coupling import (
    TimeSeriesMFT
)
from geojson_modelica_translator.model_connectors.networks import (
    NetworkChilledWaterStub,
    NetworkHeatedWaterStub
)
from geojson_modelica_translator.system_parameters.system_parameters import (
    SystemParameters
)

from ..base_test_case import TestCaseBase


class TimeSeriesModelConnectorSingleBuildingMFTETSTest(TestCaseBase):
    def setUp(self):
        super().setUp()

        project_name = "time_series_massflow"
        self.data_dir, self.output_dir = self.set_up(os.path.dirname(__file__), project_name)

        # load in the example geojson with a single office building
        filename = os.path.join(self.data_dir, "time_series_ex1.json")
        self.gj = UrbanOptGeoJson(filename)

        # load system parameter data
        filename = os.path.join(self.data_dir, "time_series_system_params_massflow_ex1.json")
        sys_params = SystemParameters(filename)

        # create the load, ETSes and their couplings
        time_series_mft_load = TimeSeriesMFT(sys_params, self.gj.buildings[0])
        geojson_load_id = self.gj.buildings[0].feature.properties["id"]

        heating_indirect_system = HeatingIndirect(sys_params, geojson_load_id)
        ts_hi_coupling = Coupling(time_series_mft_load, heating_indirect_system)

        cooling_indirect_system = CoolingIndirect(sys_params, geojson_load_id)
        ts_ci_coupling = Coupling(time_series_mft_load, cooling_indirect_system)

        # create network stubs for the ETSes
        heated_water_stub = NetworkHeatedWaterStub(sys_params)
        hi_hw_coupling = Coupling(heating_indirect_system, heated_water_stub)

        chilled_water_stub = NetworkChilledWaterStub(sys_params)
        ci_cw_coupling = Coupling(cooling_indirect_system, chilled_water_stub)

        # build the district system
        self.district = District(
            root_dir=self.output_dir,
            project_name=project_name,
            system_parameters=sys_params,
            coupling_graph=CouplingGraph([
                ts_hi_coupling,
                ts_ci_coupling,
                hi_hw_coupling,
                ci_cw_coupling,
            ])
        )
        self.district.to_modelica()

    def test_build_district_system(self):
        root_path = Path(self.district._scaffold.districts_path.files_dir).resolve()
        assert (root_path / 'DistrictEnergySystem.mo').exists()

    @pytest.mark.simulation
    def test_mft_time_series_to_modelica_and_run(self):
        root_path = Path(self.district._scaffold.districts_path.files_dir).resolve()
        mo_file_name = Path(root_path) / 'DistrictEnergySystem.mo'
        # set the run time to 31536000 (full year in seconds)
        mofile = Model(mo_file_name)
        mofile.update_model_annotation({"experiment": {"StopTime": 31536000}})
        mofile.save()

        self.run_and_assert_in_docker(
            f'{self.district._scaffold.project_name}.Districts.DistrictEnergySystem',
            file_to_load=self.district._scaffold.package_path,
            run_path=self.district._scaffold.project_path,
            start_time=0,
            stop_time=31536000,
        )

        # Check the results
        results_dir = f'{self.district._scaffold.project_path}/time_series_massflow.Districts.DistrictEnergySystem_results'

        mat_file = f'{results_dir}/time_series_massflow.Districts.DistrictEnergySystem_res.mat'
        mat_results = Reader(mat_file, 'dymola')

        # hack to get the name of the loads (rather the 8 character connector shas)
        timeseries_load_var = None
        coolflow_var = None
        heatflow_var = None
        for var in mat_results.varNames():
            m = re.match("TimeSerMFTLoa_(.{8})", var)
            if m:
                timeseries_load_var = m[1]
                continue

            m = re.match("cooInd_(.{8})", var)
            if m:
                coolflow_var = m[1]
                continue

            m = re.match("heaInd_(.{8})", var)
            if m:
                heatflow_var = m[1]
                continue

            if None not in (timeseries_load_var, coolflow_var, heatflow_var):
                break

        (time1, ts_hea_load) = mat_results.values(f"TimeSerMFTLoa_{timeseries_load_var}.ports_aChiWat[1].m_flow")
        (_time1, ts_chi_load) = mat_results.values(f"TimeSerMFTLoa_{timeseries_load_var}.ports_aHeaWat[1].m_flow")
        (_time1, cool_q_flow) = mat_results.values(f"cooInd_{coolflow_var}.Q_flow")
        (_time1, heat_q_flow) = mat_results.values(f"heaInd_{heatflow_var}.Q_flow")

        # if any of these assertions fail, then it is likely that the change in the timeseries massflow model
        # has been updated and we need to revalidate the models.
        self.assertEqual(ts_hea_load.min(), 0)
        self.assertAlmostEqual(ts_hea_load.max(), 52, delta=1)
        self.assertAlmostEqual(ts_hea_load.mean(), 4, delta=1)

        self.assertEqual(ts_chi_load.min(), 0)
        self.assertAlmostEqual(ts_chi_load.max(), 67, delta=1)
        self.assertAlmostEqual(ts_chi_load.mean(), 4, delta=1)

        self.assertAlmostEqual(cool_q_flow.min(), -131.9, delta=10)
        self.assertAlmostEqual(cool_q_flow.max(), 512936, delta=10)
        self.assertAlmostEqual(cool_q_flow.mean(), 30210, delta=10)

        self.assertAlmostEqual(heat_q_flow.min(), -342201, delta=10)
        self.assertAlmostEqual(heat_q_flow.max(), 62995, delta=10)
        self.assertAlmostEqual(heat_q_flow.mean(), -13905, delta=10)
