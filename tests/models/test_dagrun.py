#
# Licensed to the Apache Software Foundation (ASF) under one
# or more contributor license agreements.  See the NOTICE file
# distributed with this work for additional information
# regarding copyright ownership.  The ASF licenses this file
# to you under the Apache License, Version 2.0 (the
# "License"); you may not use this file except in compliance
# with the License.  You may obtain a copy of the License at
#
#   http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing,
# software distributed under the License is distributed on an
# "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY
# KIND, either express or implied.  See the License for the
# specific language governing permissions and limitations
# under the License.

import datetime
import unittest
from unittest import mock
from unittest.mock import call

import pytest
from parameterized import parameterized

from airflow import models, settings
from airflow.models import DAG, DagBag, DagModel, TaskInstance as TI, clear_task_instances
from airflow.models.dagrun import DagRun
from airflow.operators.dummy import DummyOperator
from airflow.operators.python import ShortCircuitOperator
from airflow.serialization.serialized_objects import SerializedDAG
from airflow.stats import Stats
from airflow.utils import timezone
from airflow.utils.callback_requests import DagCallbackRequest
from airflow.utils.dates import days_ago
from airflow.utils.state import State
from airflow.utils.trigger_rule import TriggerRule
from airflow.utils.types import DagRunType
from tests.models import DEFAULT_DATE
from tests.test_utils.db import clear_db_dags, clear_db_pools, clear_db_runs


class TestDagRun(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.dagbag = DagBag(include_examples=True)

    def setUp(self):
        clear_db_runs()
        clear_db_pools()
        clear_db_dags()

    def tearDown(self) -> None:
        clear_db_runs()
        clear_db_pools()
        clear_db_dags()

    def create_dag_run(
        self,
        dag,
        state=State.RUNNING,
        task_states=None,
        execution_date=None,
        is_backfill=False,
        creating_job_id=None,
    ):
        now = timezone.utcnow()
        if execution_date is None:
            execution_date = now
        if is_backfill:
            run_type = DagRunType.BACKFILL_JOB
        else:
            run_type = DagRunType.MANUAL
        dag_run = dag.create_dagrun(
            run_type=run_type,
            execution_date=execution_date,
            start_date=now,
            state=state,
            external_trigger=False,
            creating_job_id=creating_job_id,
        )

        if task_states is not None:
            session = settings.Session()
            for task_id, task_state in task_states.items():
                ti = dag_run.get_task_instance(task_id)
                ti.set_state(task_state, session)
            session.flush()

        return dag_run

    def test_clear_task_instances_for_backfill_dagrun(self):
        now = timezone.utcnow()
        session = settings.Session()
        dag_id = 'test_clear_task_instances_for_backfill_dagrun'
        dag = DAG(dag_id=dag_id, start_date=now)
        self.create_dag_run(dag, execution_date=now, is_backfill=True)

        task0 = DummyOperator(task_id='backfill_task_0', owner='test', dag=dag)
        ti0 = TI(task=task0, execution_date=now)
        ti0.run()

        qry = session.query(TI).filter(TI.dag_id == dag.dag_id).all()
        clear_task_instances(qry, session)
        session.commit()
        ti0.refresh_from_db()
        dr0 = session.query(DagRun).filter(DagRun.dag_id == dag_id, DagRun.execution_date == now).first()
        assert dr0.state == State.QUEUED

    def test_dagrun_find(self):
        session = settings.Session()
        now = timezone.utcnow()

        dag_id1 = "test_dagrun_find_externally_triggered"
        dag_run = models.DagRun(
            dag_id=dag_id1,
            run_id=dag_id1,
            run_type=DagRunType.MANUAL,
            execution_date=now,
            start_date=now,
            state=State.RUNNING,
            external_trigger=True,
        )
        session.add(dag_run)

        dag_id2 = "test_dagrun_find_not_externally_triggered"
        dag_run = models.DagRun(
            dag_id=dag_id2,
            run_id=dag_id2,
            run_type=DagRunType.MANUAL,
            execution_date=now,
            start_date=now,
            state=State.RUNNING,
            external_trigger=False,
        )
        session.add(dag_run)

        session.commit()

        assert 1 == len(models.DagRun.find(dag_id=dag_id1, external_trigger=True))
        assert 1 == len(models.DagRun.find(run_id=dag_id1))
        assert 2 == len(models.DagRun.find(run_id=[dag_id1, dag_id2]))
        assert 2 == len(models.DagRun.find(execution_date=[now, now]))
        assert 2 == len(models.DagRun.find(execution_date=now))
        assert 0 == len(models.DagRun.find(dag_id=dag_id1, external_trigger=False))
        assert 0 == len(models.DagRun.find(dag_id=dag_id2, external_trigger=True))
        assert 1 == len(models.DagRun.find(dag_id=dag_id2, external_trigger=False))

    def test_dagrun_find_duplicate(self):
        session = settings.Session()
        now = timezone.utcnow()

        dag_id = "test_dagrun_find_duplicate"
        dag_run = models.DagRun(
            dag_id=dag_id,
            run_id=dag_id,
            run_type=DagRunType.MANUAL,
            execution_date=now,
            start_date=now,
            state=State.RUNNING,
            external_trigger=True,
        )
        session.add(dag_run)

        session.commit()

        assert models.DagRun.find_duplicate(dag_id=dag_id, run_id=dag_id, execution_date=now) is not None
        assert models.DagRun.find_duplicate(dag_id=dag_id, run_id=dag_id, execution_date=None) is not None
        assert models.DagRun.find_duplicate(dag_id=dag_id, run_id=None, execution_date=now) is not None
        assert models.DagRun.find_duplicate(dag_id=dag_id, run_id=None, execution_date=None) is None

    def test_dagrun_success_when_all_skipped(self):
        """
        Tests that a DAG run succeeds when all tasks are skipped
        """
        dag = DAG(dag_id='test_dagrun_success_when_all_skipped', start_date=timezone.datetime(2017, 1, 1))
        dag_task1 = ShortCircuitOperator(
            task_id='test_short_circuit_false', dag=dag, python_callable=lambda: False
        )
        dag_task2 = DummyOperator(task_id='test_state_skipped1', dag=dag)
        dag_task3 = DummyOperator(task_id='test_state_skipped2', dag=dag)
        dag_task1.set_downstream(dag_task2)
        dag_task2.set_downstream(dag_task3)

        initial_task_states = {
            'test_short_circuit_false': State.SUCCESS,
            'test_state_skipped1': State.SKIPPED,
            'test_state_skipped2': State.SKIPPED,
        }

        dag_run = self.create_dag_run(dag=dag, state=State.RUNNING, task_states=initial_task_states)
        dag_run.update_state()
        assert State.SUCCESS == dag_run.state

    def test_dagrun_success_conditions(self):
        session = settings.Session()

        dag = DAG('test_dagrun_success_conditions', start_date=DEFAULT_DATE, default_args={'owner': 'owner1'})

        # A -> B
        # A -> C -> D
        # ordered: B, D, C, A or D, B, C, A or D, C, B, A
        with dag:
            op1 = DummyOperator(task_id='A')
            op2 = DummyOperator(task_id='B')
            op3 = DummyOperator(task_id='C')
            op4 = DummyOperator(task_id='D')
            op1.set_upstream([op2, op3])
            op3.set_upstream(op4)

        dag.clear()

        now = timezone.utcnow()
        dr = dag.create_dagrun(
            run_id='test_dagrun_success_conditions', state=State.RUNNING, execution_date=now, start_date=now
        )

        # op1 = root
        ti_op1 = dr.get_task_instance(task_id=op1.task_id)
        ti_op1.set_state(state=State.SUCCESS, session=session)

        ti_op2 = dr.get_task_instance(task_id=op2.task_id)
        ti_op3 = dr.get_task_instance(task_id=op3.task_id)
        ti_op4 = dr.get_task_instance(task_id=op4.task_id)

        # root is successful, but unfinished tasks
        dr.update_state()
        assert State.RUNNING == dr.state

        # one has failed, but root is successful
        ti_op2.set_state(state=State.FAILED, session=session)
        ti_op3.set_state(state=State.SUCCESS, session=session)
        ti_op4.set_state(state=State.SUCCESS, session=session)
        dr.update_state()
        assert State.SUCCESS == dr.state

    def test_dagrun_deadlock(self):
        session = settings.Session()
        dag = DAG('text_dagrun_deadlock', start_date=DEFAULT_DATE, default_args={'owner': 'owner1'})

        with dag:
            op1 = DummyOperator(task_id='A')
            op2 = DummyOperator(task_id='B')
            op2.trigger_rule = TriggerRule.ONE_FAILED
            op2.set_upstream(op1)

        dag.clear()
        now = timezone.utcnow()
        dr = dag.create_dagrun(
            run_id='test_dagrun_deadlock', state=State.RUNNING, execution_date=now, start_date=now
        )

        ti_op1 = dr.get_task_instance(task_id=op1.task_id)
        ti_op1.set_state(state=State.SUCCESS, session=session)
        ti_op2 = dr.get_task_instance(task_id=op2.task_id)
        ti_op2.set_state(state=State.NONE, session=session)

        dr.update_state()
        assert dr.state == State.RUNNING

        ti_op2.set_state(state=State.NONE, session=session)
        op2.trigger_rule = 'invalid'
        dr.update_state()
        assert dr.state == State.FAILED

    def test_dagrun_no_deadlock_with_shutdown(self):
        session = settings.Session()
        dag = DAG('test_dagrun_no_deadlock_with_shutdown', start_date=DEFAULT_DATE)
        with dag:
            op1 = DummyOperator(task_id='upstream_task')
            op2 = DummyOperator(task_id='downstream_task')
            op2.set_upstream(op1)

        dr = dag.create_dagrun(
            run_id='test_dagrun_no_deadlock_with_shutdown',
            state=State.RUNNING,
            execution_date=DEFAULT_DATE,
            start_date=DEFAULT_DATE,
        )
        upstream_ti = dr.get_task_instance(task_id='upstream_task')
        upstream_ti.set_state(State.SHUTDOWN, session=session)

        dr.update_state()
        assert dr.state == State.RUNNING

    def test_dagrun_no_deadlock_with_depends_on_past(self):
        session = settings.Session()
        dag = DAG('test_dagrun_no_deadlock', start_date=DEFAULT_DATE)
        with dag:
            DummyOperator(task_id='dop', depends_on_past=True)
            DummyOperator(task_id='tc', max_active_tis_per_dag=1)

        dag.clear()
        dr = dag.create_dagrun(
            run_id='test_dagrun_no_deadlock_1',
            state=State.RUNNING,
            execution_date=DEFAULT_DATE,
            start_date=DEFAULT_DATE,
        )
        dr2 = dag.create_dagrun(
            run_id='test_dagrun_no_deadlock_2',
            state=State.RUNNING,
            execution_date=DEFAULT_DATE + datetime.timedelta(days=1),
            start_date=DEFAULT_DATE + datetime.timedelta(days=1),
        )
        ti1_op1 = dr.get_task_instance(task_id='dop')
        dr2.get_task_instance(task_id='dop')
        ti2_op1 = dr.get_task_instance(task_id='tc')
        dr.get_task_instance(task_id='tc')
        ti1_op1.set_state(state=State.RUNNING, session=session)
        dr.update_state()
        dr2.update_state()
        assert dr.state == State.RUNNING
        assert dr2.state == State.RUNNING

        ti2_op1.set_state(state=State.RUNNING, session=session)
        dr.update_state()
        dr2.update_state()
        assert dr.state == State.RUNNING
        assert dr2.state == State.RUNNING

    def test_dagrun_success_callback(self):
        def on_success_callable(context):
            assert context['dag_run'].dag_id == 'test_dagrun_success_callback'

        dag = DAG(
            dag_id='test_dagrun_success_callback',
            start_date=datetime.datetime(2017, 1, 1),
            on_success_callback=on_success_callable,
        )
        dag_task1 = DummyOperator(task_id='test_state_succeeded1', dag=dag)
        dag_task2 = DummyOperator(task_id='test_state_succeeded2', dag=dag)
        dag_task1.set_downstream(dag_task2)

        initial_task_states = {
            'test_state_succeeded1': State.SUCCESS,
            'test_state_succeeded2': State.SUCCESS,
        }

        # Scheduler uses Serialized DAG -- so use that instead of the Actual DAG
        dag = SerializedDAG.from_dict(SerializedDAG.to_dict(dag))

        dag_run = self.create_dag_run(dag=dag, state=State.RUNNING, task_states=initial_task_states)
        _, callback = dag_run.update_state()
        assert State.SUCCESS == dag_run.state
        # Callbacks are not added until handle_callback = False is passed to dag_run.update_state()
        assert callback is None

    def test_dagrun_failure_callback(self):
        def on_failure_callable(context):
            assert context['dag_run'].dag_id == 'test_dagrun_failure_callback'

        dag = DAG(
            dag_id='test_dagrun_failure_callback',
            start_date=datetime.datetime(2017, 1, 1),
            on_failure_callback=on_failure_callable,
        )
        dag_task1 = DummyOperator(task_id='test_state_succeeded1', dag=dag)
        dag_task2 = DummyOperator(task_id='test_state_failed2', dag=dag)

        initial_task_states = {
            'test_state_succeeded1': State.SUCCESS,
            'test_state_failed2': State.FAILED,
        }
        dag_task1.set_downstream(dag_task2)

        # Scheduler uses Serialized DAG -- so use that instead of the Actual DAG
        dag = SerializedDAG.from_dict(SerializedDAG.to_dict(dag))

        dag_run = self.create_dag_run(dag=dag, state=State.RUNNING, task_states=initial_task_states)
        _, callback = dag_run.update_state()
        assert State.FAILED == dag_run.state
        # Callbacks are not added until handle_callback = False is passed to dag_run.update_state()
        assert callback is None

    def test_dagrun_update_state_with_handle_callback_success(self):
        def on_success_callable(context):
            assert context['dag_run'].dag_id == 'test_dagrun_update_state_with_handle_callback_success'

        dag = DAG(
            dag_id='test_dagrun_update_state_with_handle_callback_success',
            start_date=datetime.datetime(2017, 1, 1),
            on_success_callback=on_success_callable,
        )
        dag_task1 = DummyOperator(task_id='test_state_succeeded1', dag=dag)
        dag_task2 = DummyOperator(task_id='test_state_succeeded2', dag=dag)
        dag_task1.set_downstream(dag_task2)

        initial_task_states = {
            'test_state_succeeded1': State.SUCCESS,
            'test_state_succeeded2': State.SUCCESS,
        }

        # Scheduler uses Serialized DAG -- so use that instead of the Actual DAG
        dag = SerializedDAG.from_dict(SerializedDAG.to_dict(dag))

        dag_run = self.create_dag_run(dag=dag, state=State.RUNNING, task_states=initial_task_states)

        _, callback = dag_run.update_state(execute_callbacks=False)
        assert State.SUCCESS == dag_run.state
        # Callbacks are not added until handle_callback = False is passed to dag_run.update_state()

        assert callback == DagCallbackRequest(
            full_filepath=dag_run.dag.fileloc,
            dag_id="test_dagrun_update_state_with_handle_callback_success",
            run_id=dag_run.run_id,
            is_failure_callback=False,
            msg="success",
        )

    def test_dagrun_update_state_with_handle_callback_failure(self):
        def on_failure_callable(context):
            assert context['dag_run'].dag_id == 'test_dagrun_update_state_with_handle_callback_failure'

        dag = DAG(
            dag_id='test_dagrun_update_state_with_handle_callback_failure',
            start_date=datetime.datetime(2017, 1, 1),
            on_failure_callback=on_failure_callable,
        )
        dag_task1 = DummyOperator(task_id='test_state_succeeded1', dag=dag)
        dag_task2 = DummyOperator(task_id='test_state_failed2', dag=dag)
        dag_task1.set_downstream(dag_task2)

        initial_task_states = {
            'test_state_succeeded1': State.SUCCESS,
            'test_state_failed2': State.FAILED,
        }

        # Scheduler uses Serialized DAG -- so use that instead of the Actual DAG
        dag = SerializedDAG.from_dict(SerializedDAG.to_dict(dag))

        dag_run = self.create_dag_run(dag=dag, state=State.RUNNING, task_states=initial_task_states)

        _, callback = dag_run.update_state(execute_callbacks=False)
        assert State.FAILED == dag_run.state
        # Callbacks are not added until handle_callback = False is passed to dag_run.update_state()

        assert callback == DagCallbackRequest(
            full_filepath=dag_run.dag.fileloc,
            dag_id="test_dagrun_update_state_with_handle_callback_failure",
            run_id=dag_run.run_id,
            is_failure_callback=True,
            msg="task_failure",
        )

    def test_dagrun_set_state_end_date(self):
        session = settings.Session()

        dag = DAG('test_dagrun_set_state_end_date', start_date=DEFAULT_DATE, default_args={'owner': 'owner1'})

        dag.clear()

        now = timezone.utcnow()
        dr = dag.create_dagrun(
            run_id='test_dagrun_set_state_end_date', state=State.RUNNING, execution_date=now, start_date=now
        )

        # Initial end_date should be NULL
        # State.SUCCESS and State.FAILED are all ending state and should set end_date
        # State.RUNNING set end_date back to NULL
        session.add(dr)
        session.commit()
        assert dr.end_date is None

        dr.set_state(State.SUCCESS)
        session.merge(dr)
        session.commit()

        dr_database = session.query(DagRun).filter(DagRun.run_id == 'test_dagrun_set_state_end_date').one()
        assert dr_database.end_date is not None
        assert dr.end_date == dr_database.end_date

        dr.set_state(State.RUNNING)
        session.merge(dr)
        session.commit()

        dr_database = session.query(DagRun).filter(DagRun.run_id == 'test_dagrun_set_state_end_date').one()

        assert dr_database.end_date is None

        dr.set_state(State.FAILED)
        session.merge(dr)
        session.commit()
        dr_database = session.query(DagRun).filter(DagRun.run_id == 'test_dagrun_set_state_end_date').one()

        assert dr_database.end_date is not None
        assert dr.end_date == dr_database.end_date

    def test_dagrun_update_state_end_date(self):
        session = settings.Session()

        dag = DAG(
            'test_dagrun_update_state_end_date', start_date=DEFAULT_DATE, default_args={'owner': 'owner1'}
        )

        # A -> B
        with dag:
            op1 = DummyOperator(task_id='A')
            op2 = DummyOperator(task_id='B')
            op1.set_upstream(op2)

        dag.clear()

        now = timezone.utcnow()
        dr = dag.create_dagrun(
            run_id='test_dagrun_update_state_end_date',
            state=State.RUNNING,
            execution_date=now,
            start_date=now,
        )

        # Initial end_date should be NULL
        # State.SUCCESS and State.FAILED are all ending state and should set end_date
        # State.RUNNING set end_date back to NULL
        session.merge(dr)
        session.commit()
        assert dr.end_date is None

        ti_op1 = dr.get_task_instance(task_id=op1.task_id)
        ti_op1.set_state(state=State.SUCCESS, session=session)
        ti_op2 = dr.get_task_instance(task_id=op2.task_id)
        ti_op2.set_state(state=State.SUCCESS, session=session)

        dr.update_state()

        dr_database = session.query(DagRun).filter(DagRun.run_id == 'test_dagrun_update_state_end_date').one()
        assert dr_database.end_date is not None
        assert dr.end_date == dr_database.end_date

        ti_op1.set_state(state=State.RUNNING, session=session)
        ti_op2.set_state(state=State.RUNNING, session=session)
        dr.update_state()

        dr_database = session.query(DagRun).filter(DagRun.run_id == 'test_dagrun_update_state_end_date').one()

        assert dr._state == State.RUNNING
        assert dr.end_date is None
        assert dr_database.end_date is None

        ti_op1.set_state(state=State.FAILED, session=session)
        ti_op2.set_state(state=State.FAILED, session=session)
        dr.update_state()

        dr_database = session.query(DagRun).filter(DagRun.run_id == 'test_dagrun_update_state_end_date').one()

        assert dr_database.end_date is not None
        assert dr.end_date == dr_database.end_date

    def test_get_task_instance_on_empty_dagrun(self):
        """
        Make sure that a proper value is returned when a dagrun has no task instances
        """
        dag = DAG(dag_id='test_get_task_instance_on_empty_dagrun', start_date=timezone.datetime(2017, 1, 1))
        ShortCircuitOperator(task_id='test_short_circuit_false', dag=dag, python_callable=lambda: False)

        session = settings.Session()

        now = timezone.utcnow()

        # Don't use create_dagrun since it will create the task instances too which we
        # don't want
        dag_run = models.DagRun(
            dag_id=dag.dag_id,
            run_id="test_get_task_instance_on_empty_dagrun",
            run_type=DagRunType.MANUAL,
            execution_date=now,
            start_date=now,
            state=State.RUNNING,
            external_trigger=False,
        )
        session.add(dag_run)
        session.commit()

        ti = dag_run.get_task_instance('test_short_circuit_false')
        assert ti is None

    def test_get_latest_runs(self):
        session = settings.Session()
        dag = DAG(dag_id='test_latest_runs_1', start_date=DEFAULT_DATE)
        self.create_dag_run(dag, execution_date=timezone.datetime(2015, 1, 1))
        self.create_dag_run(dag, execution_date=timezone.datetime(2015, 1, 2))
        dagruns = models.DagRun.get_latest_runs(session)
        session.close()
        for dagrun in dagruns:
            if dagrun.dag_id == 'test_latest_runs_1':
                assert dagrun.execution_date == timezone.datetime(2015, 1, 2)

    def test_removed_task_instances_can_be_restored(self):
        def with_all_tasks_removed(dag):
            return DAG(dag_id=dag.dag_id, start_date=dag.start_date)

        dag = DAG('test_task_restoration', start_date=DEFAULT_DATE)
        dag.add_task(DummyOperator(task_id='flaky_task', owner='test'))

        dagrun = self.create_dag_run(dag)
        flaky_ti = dagrun.get_task_instances()[0]
        assert 'flaky_task' == flaky_ti.task_id
        assert State.NONE == flaky_ti.state

        dagrun.dag = with_all_tasks_removed(dag)

        dagrun.verify_integrity()
        flaky_ti.refresh_from_db()
        assert State.NONE == flaky_ti.state

        dagrun.dag.add_task(DummyOperator(task_id='flaky_task', owner='test'))

        dagrun.verify_integrity()
        flaky_ti.refresh_from_db()
        assert State.NONE == flaky_ti.state

    def test_already_added_task_instances_can_be_ignored(self):
        dag = DAG('triggered_dag', start_date=DEFAULT_DATE)
        dag.add_task(DummyOperator(task_id='first_task', owner='test'))

        dagrun = self.create_dag_run(dag)
        first_ti = dagrun.get_task_instances()[0]
        assert 'first_task' == first_ti.task_id
        assert State.NONE == first_ti.state

        # Lets assume that the above TI was added into DB by webserver, but if scheduler
        # is running the same method at the same time it would find 0 TIs for this dag
        # and proceeds further to create TIs. Hence mocking DagRun.get_task_instances
        # method to return an empty list of TIs.
        with mock.patch.object(DagRun, 'get_task_instances') as mock_gtis:
            mock_gtis.return_value = []
            dagrun.verify_integrity()
            first_ti.refresh_from_db()
            assert State.NONE == first_ti.state

    @parameterized.expand([(state,) for state in State.task_states])
    @mock.patch.object(settings, 'task_instance_mutation_hook', autospec=True)
    def test_task_instance_mutation_hook(self, state, mock_hook):
        def mutate_task_instance(task_instance):
            if task_instance.queue == 'queue1':
                task_instance.queue = 'queue2'
            else:
                task_instance.queue = 'queue1'

        mock_hook.side_effect = mutate_task_instance

        dag = DAG('test_task_instance_mutation_hook', start_date=DEFAULT_DATE)
        dag.add_task(DummyOperator(task_id='task_to_mutate', owner='test', queue='queue1'))

        dagrun = self.create_dag_run(dag)
        task = dagrun.get_task_instances()[0]
        session = settings.Session()
        task.state = state
        session.merge(task)
        session.commit()
        assert task.queue == 'queue2'

        dagrun.verify_integrity()
        task = dagrun.get_task_instances()[0]
        assert task.queue == 'queue1'

    @parameterized.expand(
        [
            (State.SUCCESS, True),
            (State.SKIPPED, True),
            (State.RUNNING, False),
            (State.FAILED, False),
            (State.NONE, False),
        ]
    )
    def test_depends_on_past(self, prev_ti_state, is_ti_success):
        dag_id = 'test_depends_on_past'

        dag = self.dagbag.get_dag(dag_id)
        task = dag.tasks[0]

        self.create_dag_run(dag, execution_date=timezone.datetime(2016, 1, 1, 0, 0, 0), is_backfill=True)
        self.create_dag_run(dag, execution_date=timezone.datetime(2016, 1, 2, 0, 0, 0), is_backfill=True)

        prev_ti = TI(task, timezone.datetime(2016, 1, 1, 0, 0, 0))
        ti = TI(task, timezone.datetime(2016, 1, 2, 0, 0, 0))

        prev_ti.set_state(prev_ti_state)
        ti.set_state(State.QUEUED)
        ti.run()
        assert (ti.state == State.SUCCESS) == is_ti_success

    @parameterized.expand(
        [
            (State.SUCCESS, True),
            (State.SKIPPED, True),
            (State.RUNNING, False),
            (State.FAILED, False),
            (State.NONE, False),
        ]
    )
    def test_wait_for_downstream(self, prev_ti_state, is_ti_success):
        dag_id = 'test_wait_for_downstream'
        dag = self.dagbag.get_dag(dag_id)
        upstream, downstream = dag.tasks

        # For ti.set_state() to work, the DagRun has to exist,
        # Otherwise ti.previous_ti returns an unpersisted TI
        self.create_dag_run(dag, execution_date=timezone.datetime(2016, 1, 1, 0, 0, 0), is_backfill=True)
        self.create_dag_run(dag, execution_date=timezone.datetime(2016, 1, 2, 0, 0, 0), is_backfill=True)

        prev_ti_downstream = TI(task=downstream, execution_date=timezone.datetime(2016, 1, 1, 0, 0, 0))
        ti = TI(task=upstream, execution_date=timezone.datetime(2016, 1, 2, 0, 0, 0))
        prev_ti = ti.get_previous_ti()
        prev_ti.set_state(State.SUCCESS)
        assert prev_ti.state == State.SUCCESS

        prev_ti_downstream.set_state(prev_ti_state)
        ti.set_state(State.QUEUED)
        ti.run()
        assert (ti.state == State.SUCCESS) == is_ti_success

    @parameterized.expand([(State.QUEUED,), (State.RUNNING,)])
    def test_next_dagruns_to_examine_only_unpaused(self, state):
        """
        Check that "next_dagruns_to_examine" ignores runs from paused/inactive DAGs
        and gets running/queued dagruns
        """

        dag = DAG(dag_id='test_dags', start_date=DEFAULT_DATE)
        DummyOperator(task_id='dummy', dag=dag, owner='airflow')

        session = settings.Session()
        orm_dag = DagModel(
            dag_id=dag.dag_id,
            has_task_concurrency_limits=False,
            next_dagrun=DEFAULT_DATE,
            next_dagrun_create_after=DEFAULT_DATE + datetime.timedelta(days=1),
            is_active=True,
        )
        session.add(orm_dag)
        session.flush()
        dr = dag.create_dagrun(
            run_type=DagRunType.SCHEDULED,
            state=state,
            execution_date=DEFAULT_DATE,
            start_date=DEFAULT_DATE if state == State.RUNNING else None,
            session=session,
        )

        runs = DagRun.next_dagruns_to_examine(state, session).all()

        assert runs == [dr]

        orm_dag.is_paused = True
        session.flush()

        runs = DagRun.next_dagruns_to_examine(state, session).all()
        assert runs == []

    @mock.patch.object(Stats, 'timing')
    def test_no_scheduling_delay_for_nonscheduled_runs(self, stats_mock):
        """
        Tests that dag scheduling delay stat is not called if the dagrun is not a scheduled run.
        This case is manual run. Simple test for coherence check.
        """
        dag = DAG(dag_id='test_dagrun_stats', start_date=days_ago(1))
        dag_task = DummyOperator(task_id='dummy', dag=dag)

        initial_task_states = {
            dag_task.task_id: State.SUCCESS,
        }

        dag_run = self.create_dag_run(dag=dag, state=State.RUNNING, task_states=initial_task_states)
        dag_run.update_state()
        assert call(f'dagrun.{dag.dag_id}.first_task_scheduling_delay') not in stats_mock.mock_calls

    @parameterized.expand(
        [
            ("*/5 * * * *", True),
            (None, False),
            ("@once", False),
        ]
    )
    def test_emit_scheduling_delay(self, schedule_interval, expected):
        """
        Tests that dag scheduling delay stat is set properly once running scheduled dag.
        dag_run.update_state() invokes the _emit_true_scheduling_delay_stats_for_finished_state method.
        """
        dag = DAG(dag_id='test_emit_dag_stats', start_date=days_ago(1), schedule_interval=schedule_interval)
        dag_task = DummyOperator(task_id='dummy', dag=dag, owner='airflow')

        session = settings.Session()
        try:
            info = dag.next_dagrun_info(None)
            orm_dag_kwargs = {"dag_id": dag.dag_id, "has_task_concurrency_limits": False, "is_active": True}
            if info is not None:
                orm_dag_kwargs.update(
                    {
                        "next_dagrun": info.logical_date,
                        "next_dagrun_data_interval": info.data_interval,
                        "next_dagrun_create_after": info.run_after,
                    },
                )
            orm_dag = DagModel(**orm_dag_kwargs)
            session.add(orm_dag)
            session.flush()
            dag_run = dag.create_dagrun(
                run_type=DagRunType.SCHEDULED,
                state=State.SUCCESS,
                execution_date=dag.start_date,
                start_date=dag.start_date,
                session=session,
            )
            ti = dag_run.get_task_instance(dag_task.task_id, session)
            ti.set_state(State.SUCCESS, session)
            session.flush()

            with mock.patch.object(Stats, 'timing') as stats_mock:
                dag_run.update_state(session)

            metric_name = f'dagrun.{dag.dag_id}.first_task_scheduling_delay'

            if expected:
                true_delay = ti.start_date - dag_run.data_interval_end
                sched_delay_stat_call = call(metric_name, true_delay)
                assert sched_delay_stat_call in stats_mock.mock_calls
            else:
                # Assert that we never passed the metric
                sched_delay_stat_call = call(metric_name, mock.ANY)
                assert sched_delay_stat_call not in stats_mock.mock_calls
        finally:
            # Don't write anything to the DB
            session.rollback()
            session.close()

    def test_states_sets(self):
        """
        Tests that adding State.failed_states and State.success_states work as expected.
        """
        dag = DAG(dag_id='test_dagrun_states', start_date=days_ago(1))
        dag_task_success = DummyOperator(task_id='dummy', dag=dag)
        dag_task_failed = DummyOperator(task_id='dummy2', dag=dag)

        initial_task_states = {
            dag_task_success.task_id: State.SUCCESS,
            dag_task_failed.task_id: State.FAILED,
        }
        dag_run = self.create_dag_run(dag=dag, state=State.RUNNING, task_states=initial_task_states)
        ti_success = dag_run.get_task_instance(dag_task_success.task_id)
        ti_failed = dag_run.get_task_instance(dag_task_failed.task_id)
        assert ti_success.state in State.success_states
        assert ti_failed.state in State.failed_states


@pytest.mark.parametrize(
    ('run_type', 'expected_tis'),
    [
        pytest.param(DagRunType.MANUAL, 1, id='manual'),
        pytest.param(DagRunType.BACKFILL_JOB, 2, id='backfill'),
    ],
)
@mock.patch.object(Stats, 'incr')
def test_verify_integrity_task_start_date(Stats_incr, session, run_type, expected_tis):
    """Test that tasks with specific start dates are only created for backfill runs"""
    with DAG('test', start_date=DEFAULT_DATE) as dag:
        DummyOperator(task_id='without')
        DummyOperator(task_id='with_startdate', start_date=DEFAULT_DATE + datetime.timedelta(1))

    dag_run = DagRun(
        dag_id=dag.dag_id,
        run_type=run_type,
        execution_date=DEFAULT_DATE,
        run_id=DagRun.generate_run_id(run_type, DEFAULT_DATE),
    )
    dag_run.dag = dag

    session.add(dag_run)
    session.flush()
    dag_run.verify_integrity(session)

    tis = dag_run.task_instances
    assert len(tis) == expected_tis

    Stats_incr.assert_called_with('task_instance_created-DummyOperator', expected_tis)
