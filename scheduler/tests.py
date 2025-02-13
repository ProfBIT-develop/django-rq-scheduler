# -*- coding: utf-8 -*-
import zoneinfo
from datetime import datetime, timedelta
from itertools import combinations

import factory
from django.conf import settings
from django.contrib.auth.models import User
from django.contrib.contenttypes.models import ContentType
from django.core.exceptions import ValidationError
from django.test import Client
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone
from django_rq import job as jobdecorator
from django_rq.queues import get_queue

from scheduler.models import BaseJob, BaseJobArg, CronJob, JobArg, JobKwarg, RepeatableJob, ScheduledJob
from scheduler.scheduler import DjangoRQScheduler


# RQ
# Configuration to pretend there is a Redis service available.
# Set up the connection before RQ Django reads the settings.
# The connection must be the same because in fakeredis connections
# do not share the state. Therefore, we define a singleton object to reuse it.

class BaseJobFactory(factory.django.DjangoModelFactory):
    name = factory.Sequence(lambda n: 'Scheduled Job %d' % n)
    job_id = None
    queue = list(settings.RQ_QUEUES.keys())[0]
    callable = 'scheduler.tests.test_job'
    enabled = True
    timeout = None

    class Meta:
        django_get_or_create = ('name',)
        abstract = True


class ScheduledJobFactory(BaseJobFactory):
    result_ttl = None

    @factory.lazy_attribute
    def scheduled_time(self):
        return timezone.now() + timedelta(days=1)

    class Meta:
        model = ScheduledJob


class RepeatableJobFactory(BaseJobFactory):
    result_ttl = None
    interval = 1
    interval_unit = 'hours'
    repeat = None

    @factory.lazy_attribute
    def scheduled_time(self):
        return timezone.now() + timedelta(minutes=1)

    class Meta:
        model = RepeatableJob


class CronJobFactory(BaseJobFactory):
    cron_string = "0 0 * * *"
    repeat = None

    class Meta:
        model = CronJob


class BaseJobArgFactory(factory.django.DjangoModelFactory):
    arg_type = 'str_val'
    str_val = ''
    int_val = None
    bool_val = False
    datetime_val = None
    object_id = factory.SelfAttribute('content_object.id')
    content_type = factory.LazyAttribute(
        lambda o: ContentType.objects.get_for_model(o.content_object))
    content_object = factory.SubFactory(ScheduledJobFactory)

    class Meta:
        exclude = ['content_object']
        abstract = True


class JobArgFactory(BaseJobArgFactory):
    class Meta:
        model = JobArg


class JobKwargFactory(BaseJobArgFactory):
    key = factory.Sequence(lambda n: 'key%d' % n)

    class Meta:
        model = JobKwarg


@jobdecorator
def test_job():
    return 1 + 1


@jobdecorator
def test_args_kwargs(*args, **kwargs):
    func = "test_args_kwargs({})"
    args_list = [repr(arg) for arg in args]
    kwargs_list = [k + '=' + repr(v) for (k, v) in kwargs.items()]
    return func.format(', '.join(args_list + kwargs_list))


test_non_callable = 'I am a teapot'


def _get_job_from_queue(django_job):
    queue = django_job.get_rqueue()
    jobs_to_schedule = queue.scheduled_job_registry.get_job_ids()
    entry = next(i for i in jobs_to_schedule if i == django_job.job_id)
    return queue.fetch_job(entry)


class BaseTestCases:
    class TestBaseJobArg(TestCase):
        JobArgClass = BaseJobArg
        JobArgClassFactory = BaseJobArgFactory

        def test_clean_no_values(self):
            arg = self.JobArgClassFactory()
            with self.assertRaises(ValidationError):
                arg.clean_one_value()

        def test_clean_one_value(self):
            test_kwargs = {'int_val': 1, 'bool_val': True, 'datetime_val': timezone.now(), 'str_val': 'not blank'}
            for kwarg_set in combinations(test_kwargs, 1):
                arg = self.JobArgClassFactory(**{k: v for k, v in test_kwargs.items() if k in kwarg_set})
                try:
                    arg.clean_one_value()
                except ValidationError as e:
                    self.assertTrue(False, msg=e)

        # False bool values are ignored when it's not the arg_type
        def test_clean_multiple_values(self):
            test_kwargs = {'int_val': 1, 'datetime_val': timezone.now(), 'str_val': 'not blank'}
            for k in range(2, len(test_kwargs) + 1):
                for kwarg_set in combinations(test_kwargs, k):
                    arg = self.JobArgClassFactory(**{k: v for k, v in test_kwargs.items() if k in kwarg_set})
                    with self.assertRaises(ValidationError):
                        arg.clean_one_value()

        def test_clean_multiple_values_with_bool(self):
            test_kwargs = {'int_val': 1, 'bool_val': True, 'datetime_val': timezone.now(), 'str_val': 'not blank'}
            for k in range(2, len(test_kwargs) + 1):
                for kwarg_set in combinations(test_kwargs, k):
                    arg = self.JobArgClassFactory(**{k: v for k, v in test_kwargs.items() if k in kwarg_set})
                    with self.assertRaises(ValidationError):
                        arg.clean_one_value()

        def test_clean_one_value_invalid_str_int(self):
            arg = self.JobArgClassFactory(str_val='not blank', int_val=1, datetime_val=None)
            with self.assertRaises(ValidationError):
                arg.clean_one_value()

        def test_clean_one_value_invalid_str_datetime(self):
            arg = self.JobArgClassFactory(str_val='not blank', int_val=None, datetime_val=timezone.now())
            with self.assertRaises(ValidationError):
                arg.clean_one_value()

        def test_clean_one_value_invalid_int_datetime(self):
            arg = self.JobArgClassFactory(str_val='', int_val=1, datetime_val=timezone.now())
            with self.assertRaises(ValidationError):
                arg.clean_one_value()

        def test_clean_one_value_valid_bool(self):
            arg = self.JobArgClassFactory(arg_type='bool_val')
            try:
                arg.clean_one_value()
            except ValidationError as e:
                self.fail(e)
            arg = self.JobArgClassFactory(arg_type='bool_val', bool_val=True)
            try:
                arg.clean_one_value()
            except ValidationError as e:
                self.fail(e)

        def test_clean_invalid(self):
            arg = self.JobArgClassFactory(str_val='str', int_val=1, datetime_val=timezone.now())
            with self.assertRaises(ValidationError):
                arg.clean()

        def test_clean(self):
            arg = self.JobArgClassFactory(str_val='something')
            self.assertIsNone(arg.clean())

    class TestBaseJob(TestCase):
        JobClass = BaseJob
        JobClassFactory = BaseJobFactory

        @classmethod
        def setUpTestData(cls) -> None:
            try:
                User.objects.create_superuser('admin', 'admin@a.com', 'admin')
            except Exception:
                pass
            cls.client = Client()

        def test_callable_func(self):
            job = self.JobClass()
            job.callable = 'scheduler.tests.test_job'
            func = job.callable_func()
            self.assertEqual(test_job, func)

        def test_callable_func_not_callable(self):
            job = self.JobClass()
            job.callable = 'scheduler.tests.test_non_callable'
            with self.assertRaises(TypeError):
                job.callable_func()

        def test_clean_callable(self):
            job = self.JobClass()
            job.callable = 'scheduler.tests.test_job'
            self.assertIsNone(job.clean_callable())

        def test_clean_callable_invalid(self):
            job = self.JobClass()
            job.callable = 'scheduler.tests.test_non_callable'
            with self.assertRaises(ValidationError):
                job.clean_callable()

        def test_clean_queue(self):
            for queue in settings.RQ_QUEUES.keys():
                job = self.JobClass()
                job.queue = queue
                self.assertIsNone(job.clean_queue())

        def test_clean_queue_invalid(self):
            job = self.JobClass()
            job.queue = 'xxxxxx'
            job.callable = 'scheduler.tests.test_job'
            with self.assertRaises(ValidationError):
                job.clean()

        # next 2 check the above are included in job.clean() function
        def test_clean(self):
            job = self.JobClass()
            job.queue = list(settings.RQ_QUEUES)[0]
            job.callable = 'scheduler.tests.test_job'
            self.assertIsNone(job.clean())

        def test_clean_invalid_callable(self):
            job = self.JobClass()
            job.queue = list(settings.RQ_QUEUES)[0]
            job.callable = 'scheduler.tests.test_non_callable'
            with self.assertRaises(ValidationError):
                job.clean()

        def test_clean_invalid_queue(self):
            job = self.JobClass()
            job.queue = 'xxxxxx'
            job.callable = 'scheduler.tests.test_job'
            with self.assertRaises(ValidationError):
                job.clean()

        def test_is_schedulable_already_scheduled(self):
            job = self.JobClassFactory()
            job.schedule()
            self.assertFalse(job.is_schedulable())

        def test_is_schedulable_disabled(self):
            job = self.JobClass()
            job.enabled = False
            self.assertFalse(job.is_schedulable())

        def test_is_schedulable_enabled(self):
            job = self.JobClass()
            job.enabled = True
            self.assertTrue(job.is_schedulable())

        def test_schedule(self):
            job = self.JobClassFactory()
            self.assertTrue(job.is_scheduled())
            self.assertIsNotNone(job.job_id)

        def test_unschedulable(self):
            job = self.JobClassFactory(enabled=False)
            self.assertFalse(job.is_scheduled())
            self.assertIsNone(job.job_id)

        def test_unschedule(self):
            job = self.JobClassFactory()
            self.assertTrue(job.unschedule())
            self.assertIsNone(job.job_id)

        def test_unschedule_not_scheduled(self):
            job = self.JobClassFactory(enabled=False)
            self.assertTrue(job.unschedule())
            self.assertIsNone(job.job_id)

        def test_save_enabled(self):
            job = self.JobClassFactory()
            job.save()
            self.assertIsNotNone(job.job_id)

        def test_save_disabled(self):
            job = self.JobClassFactory(enabled=False)
            job.save()
            self.assertIsNone(job.job_id)

        def test_save_and_schedule(self):
            job = self.JobClassFactory()
            self.assertIsNotNone(job.job_id)
            self.assertTrue(job.is_scheduled())

        def test_schedule2(self):
            job = self.JobClass()
            job.queue = list(settings.RQ_QUEUES)[0]
            job.enabled = False
            job.scheduled_time = timezone.now() + timedelta(minutes=1)
            self.assertFalse(job.schedule())

        def test_delete_and_unschedule(self):
            job = self.JobClassFactory()
            self.assertIsNotNone(job.job_id)
            self.assertTrue(job.is_scheduled())
            job.delete()
            self.assertFalse(job.is_scheduled())

        def test_job_build(self):
            prev_count = self.JobClass.objects.count()
            self.JobClassFactory.build()
            self.assertEqual(self.JobClass.objects.count(), prev_count)

        def test_job_create(self):
            prev_count = self.JobClass.objects.count()
            self.JobClassFactory.create()
            self.assertEqual(self.JobClass.objects.count(), prev_count + 1)

        def test_str(self):
            name = "test"
            job = self.JobClassFactory(name=name)
            self.assertEqual(str(job), name)

        def test_callable_passthrough(self):
            job = self.JobClassFactory()
            entry = _get_job_from_queue(job)
            self.assertEqual(entry.func, test_job)

        def test_timeout_passthrough(self):
            job = self.JobClassFactory(timeout=500)
            entry = _get_job_from_queue(job)
            self.assertEqual(entry.timeout, 500)

        def test_at_front_passthrough(self):
            job = self.JobClassFactory(at_front=True)
            queue = job.get_rqueue()
            jobs_to_schedule = queue.scheduled_job_registry.get_job_ids()
            self.assertIn(job.job_id, jobs_to_schedule)

        def test_callable_result(self):
            job = self.JobClassFactory()
            entry = _get_job_from_queue(job)
            self.assertEqual(entry.perform(), 2)

        def test_callable_empty_args_and_kwargs(self):
            job = self.JobClassFactory(callable='scheduler.tests.test_args_kwargs')
            entry = _get_job_from_queue(job)
            self.assertEqual(entry.perform(), 'test_args_kwargs()')

        def test_delete_args(self):
            job = self.JobClassFactory()
            arg = JobArgFactory(str_val='one', content_object=job)
            self.assertEqual(1, job.callable_args.count())
            arg.delete()
            self.assertEqual(0, job.callable_args.count())

        def test_delete_kwargs(self):
            job = self.JobClassFactory()
            kwarg = JobKwargFactory(key='key1', arg_type='str_val', str_val='one', content_object=job)
            self.assertEqual(1, job.callable_kwargs.count())
            kwarg.delete()
            self.assertEqual(0, job.callable_kwargs.count())

        def test_parse_args(self):
            job = self.JobClassFactory()
            date = timezone.now()
            JobArgFactory(str_val='one', content_object=job)
            JobArgFactory(arg_type='int_val', int_val=2, content_object=job)
            JobArgFactory(arg_type='bool_val', bool_val=True, content_object=job)
            JobArgFactory(arg_type='bool_val', bool_val=False, content_object=job)
            JobArgFactory(arg_type='datetime_val', datetime_val=date, content_object=job)
            self.assertEqual(job.parse_args(), ['one', 2, True, False, date])

        def test_parse_kwargs(self):
            job = self.JobClassFactory()
            date = timezone.now()
            JobKwargFactory(key='key1', arg_type='str_val', str_val='one', content_object=job)
            JobKwargFactory(key='key2', arg_type='int_val', int_val=2, content_object=job)
            JobKwargFactory(key='key3', arg_type='bool_val', bool_val=True, content_object=job)
            JobKwargFactory(key='key4', arg_type='datetime_val', datetime_val=date, content_object=job)
            kwargs = job.schedule_kwargs()['kwargs']
            self.assertEqual(kwargs, dict(key1='one', key2=2, key3=True, key4=date))

        def test_callable_args_and_kwargs(self):
            job = self.JobClassFactory(callable='scheduler.tests.test_args_kwargs')
            date = timezone.now()
            JobArgFactory(arg_type='str_val', str_val='one', content_object=job)
            JobKwargFactory(key='key1', arg_type='int_val', int_val=2, content_object=job)
            JobKwargFactory(key='key2', arg_type='datetime_val', datetime_val=date, content_object=job)
            JobKwargFactory(key='key3', arg_type='bool_val', bool_val=False, content_object=job)
            job.save()
            entry = _get_job_from_queue(job)
            self.assertEqual(entry.perform(),
                             "test_args_kwargs('one', key1=2, key2={}, key3=False)".format(repr(date)))

        def test_function_string(self):
            job = self.JobClassFactory()
            date = timezone.now()
            JobArgFactory(arg_type='str_val', str_val='one', content_object=job)
            JobArgFactory(arg_type='int_val', int_val=1, content_object=job)
            JobArgFactory(arg_type='datetime_val', datetime_val=date, content_object=job)
            JobArgFactory(arg_type='bool_val', bool_val=True, content_object=job)
            JobKwargFactory(key='key1', arg_type='str_val', str_val='one', content_object=job)
            JobKwargFactory(key='key2', arg_type='int_val', int_val=2, content_object=job)
            JobKwargFactory(key='key3', arg_type='datetime_val', datetime_val=date, content_object=job)
            JobKwargFactory(key='key4', arg_type='bool_val', bool_val=False, content_object=job)
            self.assertEqual(job.function_string(),
                             ("scheduler.tests.test_job(\u200b'one', 1, {date}, True, " +
                              "key1='one', key2=2, key3={date}, key4=False)").format(date=repr(date)))

        def test_admin_list_view(self):
            # arrange
            self.client.login(username='admin', password='admin')
            job = self.JobClassFactory()
            job.save()
            model = job._meta.model.__name__.lower()
            url = reverse(f'admin:scheduler_{model}_changelist')
            # act
            res = self.client.get(url)
            # assert
            self.assertEqual(200, res.status_code)

        def test_admin_list_view_delete_model(self):
            # arrange
            self.client.login(username='admin', password='admin')
            job = self.JobClassFactory()
            job.save()
            model = job._meta.model.__name__.lower()
            url = reverse(f'admin:scheduler_{model}_changelist')
            # act
            res = self.client.post(url, data={
                'action': 'delete_model',
                '_selected_action': [job.pk, ],
            })
            # assert
            self.assertEqual(302, res.status_code)

        def test_admin_single_view(self):
            # arrange
            self.client.login(username='admin', password='admin')
            job = self.JobClassFactory()
            job.save()
            model = job._meta.model.__name__.lower()
            url = reverse(f'admin:scheduler_{model}_change', args=[job.pk, ])
            # act
            res = self.client.get(url)
            # assert
            self.assertEqual(200, res.status_code)

        def test_admin_single_delete(self):
            # arrange
            self.client.login(username='admin', password='admin')
            job = self.JobClassFactory()
            job.save()
            model = job._meta.model.__name__.lower()
            url = reverse(f'admin:scheduler_{model}_delete', args=[job.pk, ])
            # act
            res = self.client.post(url)
            # assert
            self.assertEqual(200, res.status_code)

        def test_admin_run_job_now(self):
            # arrange
            self.client.login(username='admin', password='admin')
            job = self.JobClassFactory()
            job.save()
            data = {
                'action': 'run_job_now',
                '_selected_action': [job.id, ],
            }
            model = job._meta.model.__name__.lower()
            url = reverse(f'admin:scheduler_{model}_changelist')
            # act
            res = self.client.post(url, data=data, follow=True)
            # assert
            entry = _get_job_from_queue(job)
            self.assertEqual(entry.func, test_job)
            self.assertEqual(200, res.status_code)

        def test_admin_enable_job(self):
            # arrange
            self.client.login(username='admin', password='admin')
            job = self.JobClassFactory(enabled=False)
            job.save()
            data = {
                'action': 'enable_selected',
                '_selected_action': [job.id, ],
            }
            model = job._meta.model.__name__.lower()
            url = reverse(f'admin:scheduler_{model}_changelist')
            # act
            res = self.client.post(url, data=data, follow=True)
            # assert
            self.assertEqual(200, res.status_code)
            job = self.JobClass.objects.filter(id=job.id).first()
            self.assertTrue(job.enabled)

        def test_admin_disable_job(self):
            # arrange
            self.client.login(username='admin', password='admin')
            job = self.JobClassFactory(enabled=True)
            job.save()
            data = {
                'action': 'disable_selected',
                '_selected_action': [job.id, ],
            }
            model = job._meta.model.__name__.lower()
            url = reverse(f'admin:scheduler_{model}_changelist')
            # act
            res = self.client.post(url, data=data, follow=True)
            # assert
            self.assertEqual(200, res.status_code)
            job = self.JobClass.objects.filter(id=job.id).first()
            self.assertFalse(job.enabled)

    class TestSchedulableJob(TestBaseJob):
        # Currently ScheduledJob and RepeatableJob
        JobClass = BaseJob
        JobClassFactory = BaseJobFactory

        def test_schedule_time_utc(self):
            job = self.JobClass()
            est = zoneinfo.ZoneInfo('US/Eastern')
            scheduled_time = datetime(2016, 12, 25, 8, 0, 0, tzinfo=est)
            job.scheduled_time = scheduled_time
            utc = zoneinfo.ZoneInfo('UTC')
            expected = scheduled_time.astimezone(utc).isoformat()
            self.assertEqual(expected, job.schedule_time_utc().isoformat())

        def test_result_ttl_passthrough(self):
            job = self.JobClassFactory(result_ttl=500)
            entry = _get_job_from_queue(job)
            self.assertEqual(entry.result_ttl, 500)


class TestJobArg(BaseTestCases.TestBaseJobArg):
    JobArgClass = JobArg
    JobArgClassFactory = JobArgFactory

    def test_value(self):
        arg = self.JobArgClassFactory(arg_type='str_val', str_val='something')
        self.assertEqual(arg.value(), 'something')

    def test__str__str_val(self):
        arg = self.JobArgClassFactory(arg_type='str_val', str_val='something')
        self.assertEqual('something', str(arg))

    def test__str__int_val(self):
        arg = self.JobArgClassFactory(arg_type='int_val', int_val=1)
        self.assertEqual('1', str(arg))

    def test__str__datetime_val(self):
        time = timezone.now()
        arg = self.JobArgClassFactory(arg_type='datetime_val', datetime_val=time)
        self.assertEqual(str(time), str(arg))

    def test__str__bool_val(self):
        arg = self.JobArgClassFactory(arg_type='bool_val', bool_val=True)
        self.assertEqual('True', str(arg))

    def test__repr__str_val(self):
        arg = self.JobArgClassFactory(arg_type='str_val', str_val='something')
        self.assertEqual("'something'", repr(arg))

    def test__repr__int_val(self):
        arg = self.JobArgClassFactory(arg_type='int_val', int_val=1)
        self.assertEqual('1', repr(arg))

    def test__repr__datetime_val(self):
        time = timezone.now()
        arg = self.JobArgClassFactory(arg_type='datetime_val', datetime_val=time)
        self.assertEqual(repr(time), repr(arg))

    def test__repr__bool_val(self):
        arg = self.JobArgClassFactory(arg_type='bool_val', bool_val=False)
        self.assertEqual('False', repr(arg))


class TestJobKwarg(BaseTestCases.TestBaseJobArg):
    JobArgClass = JobKwarg
    JobArgClassFactory = JobKwargFactory

    def test_value(self):
        kwarg = self.JobArgClassFactory(key='key', arg_type='str_val', str_val='value')
        self.assertEqual(kwarg.value(), ('key', 'value'))

    def test__str__str_val(self):
        kwarg = self.JobArgClassFactory(key='key', arg_type='str_val', str_val='something')
        self.assertEqual("key=key value=something", str(kwarg))

    def test__str__int_val(self):
        kwarg = self.JobArgClassFactory(key='key', arg_type='int_val', int_val=1)
        self.assertEqual("key=key value=1", str(kwarg))

    def test__str__datetime_val(self):
        time = timezone.now()
        kwarg = self.JobArgClassFactory(key='key', arg_type='datetime_val', datetime_val=time)
        self.assertEqual("key=key value={}".format(time), str(kwarg))

    def test__str__bool_val(self):
        kwarg = self.JobArgClassFactory(key='key', arg_type='bool_val', bool_val=True)
        self.assertEqual("key=key value=True", str(kwarg))

    def test__repr__str_val(self):
        kwarg = self.JobArgClassFactory(key='key', arg_type='str_val', str_val='something')
        self.assertEqual("('key', 'something')", repr(kwarg))

    def test__repr__int_val(self):
        kwarg = self.JobArgClassFactory(key='key', arg_type='int_val', int_val=1)
        self.assertEqual("('key', 1)", repr(kwarg))

    def test__repr__datetime_val(self):
        time = timezone.now()
        kwarg = self.JobArgClassFactory(key='key', arg_type='datetime_val', datetime_val=time)
        self.assertEqual("('key', {})".format(repr(time)), repr(kwarg))

    def test__repr__bool_val(self):
        kwarg = self.JobArgClassFactory(key='key', arg_type='bool_val', bool_val=True)
        self.assertEqual("('key', True)", repr(kwarg))


class TestScheduledJob(BaseTestCases.TestSchedulableJob):
    JobClass = ScheduledJob
    JobClassFactory = ScheduledJobFactory

    def test_clean(self):
        job = self.JobClass()
        job.queue = list(settings.RQ_QUEUES)[0]
        job.callable = 'scheduler.tests.test_job'
        self.assertIsNone(job.clean())

    def test_unschedulable_old_job(self):
        job = self.JobClassFactory(scheduled_time=timezone.now() - timedelta(hours=1))
        self.assertFalse(job.is_scheduled())


class TestRepeatableJob(BaseTestCases.TestSchedulableJob):
    JobClass = RepeatableJob
    JobClassFactory = RepeatableJobFactory

    def test_unschedulable_old_job(self):
        job = self.JobClassFactory(scheduled_time=timezone.now() - timedelta(hours=1), repeat=0)
        self.assertFalse(job.is_scheduled())

    def test_schedulable_old_job_repeat_none(self):
        # If repeat is None, the job should be scheduled
        job = self.JobClassFactory(scheduled_time=timezone.now() - timedelta(hours=1), repeat=None)
        self.assertTrue(job.is_scheduled())

    def test_clean(self):
        job = self.JobClass()
        job.queue = list(settings.RQ_QUEUES)[0]
        job.callable = 'scheduler.tests.test_job'
        job.interval = 1
        job.result_ttl = -1
        self.assertIsNone(job.clean())

    def test_clean_seconds(self):
        job = self.JobClass()
        job.queue = list(settings.RQ_QUEUES)[0]
        job.callable = 'scheduler.tests.test_job'
        job.interval = 60
        job.result_ttl = -1
        job.interval_unit = 'seconds'
        self.assertIsNone(job.clean())

    def test_clean_too_frequent(self):
        job = self.JobClass()
        job.queue = list(settings.RQ_QUEUES)[0]
        job.callable = 'scheduler.tests.test_job'
        job.interval = 10
        job.result_ttl = -1
        job.interval_unit = 'seconds'
        with self.assertRaises(ValidationError):
            job.clean_interval_unit()

    def test_clean_not_multiple(self):
        job = self.JobClass()
        job.queue = list(settings.RQ_QUEUES)[0]
        job.callable = 'scheduler.tests.test_job'
        job.interval = 121
        job.interval_unit = 'seconds'
        with self.assertRaises(ValidationError):
            job.clean_interval_unit()

    def test_clean_short_result_ttl(self):
        job = self.JobClass()
        job.queue = list(settings.RQ_QUEUES)[0]
        job.callable = 'scheduler.tests.test_job'
        job.interval = 1
        job.repeat = 1
        job.result_ttl = 3599
        job.interval_unit = 'hours'
        job.repeat = 42
        with self.assertRaises(ValidationError):
            job.clean_result_ttl()

    def test_clean_indefinite_result_ttl(self):
        job = self.JobClass()
        job.queue = list(settings.RQ_QUEUES)[0]
        job.callable = 'scheduler.tests.test_job'
        job.interval = 1
        job.result_ttl = -1
        job.interval_unit = 'hours'
        job.clean_result_ttl()

    def test_clean_undefined_result_ttl(self):
        job = self.JobClass()
        job.queue = list(settings.RQ_QUEUES)[0]
        job.callable = 'scheduler.tests.test_job'
        job.interval = 1
        job.interval_unit = 'hours'
        job.clean_result_ttl()

    def test_interval_seconds_weeks(self):
        job = RepeatableJobFactory(interval=2, interval_unit='weeks')
        self.assertEqual(1209600.0, job.interval_seconds())

    def test_interval_seconds_days(self):
        job = RepeatableJobFactory(interval=2, interval_unit='days')
        self.assertEqual(172800.0, job.interval_seconds())

    def test_interval_seconds_hours(self):
        job = RepeatableJobFactory(interval=2, interval_unit='hours')
        self.assertEqual(7200.0, job.interval_seconds())

    def test_interval_seconds_minutes(self):
        job = RepeatableJobFactory(interval=15, interval_unit='minutes')
        self.assertEqual(900.0, job.interval_seconds())

    def test_interval_seconds_seconds(self):
        job = RepeatableJob(interval=15, interval_unit='seconds')
        self.assertEqual(15.0, job.interval_seconds())

    def test_interval_display(self):
        job = RepeatableJobFactory(interval=15, interval_unit='minutes')
        self.assertEqual(job.interval_display(), '15 minutes')

    def test_result_interval(self):
        job = self.JobClassFactory()
        entry = _get_job_from_queue(job)
        self.assertEqual(entry.meta['interval'], 3600)

    def test_repeat(self):
        job = self.JobClassFactory(repeat=10)
        entry = _get_job_from_queue(job)
        self.assertEqual(entry.meta['repeat'], 10)

    def test_repeat_old_job_exhausted(self):
        base_time = timezone.now()
        job = self.JobClassFactory(scheduled_time=base_time - timedelta(hours=10), repeat=10)
        self.assertEqual(job.is_scheduled(), False)

    def test_repeat_old_job_last_iter(self):
        base_time = timezone.now()
        job = self.JobClassFactory(scheduled_time=base_time - timedelta(hours=9, minutes=30), repeat=10)
        self.assertEqual(job.repeat, 0)
        self.assertEqual(job.is_scheduled(), True)

    def test_repeat_old_job_remaining(self):
        base_time = timezone.now()
        job = self.JobClassFactory(scheduled_time=base_time - timedelta(minutes=30), repeat=5)
        self.assertEqual(job.repeat, 4)
        self.assertEqual(job.scheduled_time, base_time + timedelta(minutes=30))
        self.assertEqual(job.is_scheduled(), True)

    def test_repeat_none_interval_2_min(self):
        base_time = timezone.now()
        job = self.JobClassFactory(scheduled_time=base_time - timedelta(minutes=29), repeat=None)
        job.interval = 120
        job.interval_unit = 'seconds'
        job.schedule()
        self.assertTrue(job.scheduled_time > base_time)
        self.assertTrue(job.is_scheduled())

    def test_check_rescheduled_after_execution(self):
        job = self.JobClassFactory(scheduled_time=timezone.now() + timedelta(seconds=1))
        queue = job.get_rqueue()
        first_run_id = job.job_id
        entry = queue.fetch_job(first_run_id)
        queue.run_sync(entry)
        job.refresh_from_db()
        self.assertTrue(job.is_scheduled())
        self.assertNotEquals(job.job_id, first_run_id)


class TestCronJob(BaseTestCases.TestBaseJob):
    JobClass = CronJob
    JobClassFactory = CronJobFactory

    def test_clean(self):
        job = self.JobClass()
        job.cron_string = '* * * * *'
        job.queue = list(settings.RQ_QUEUES)[0]
        job.callable = 'scheduler.tests.test_job'
        self.assertIsNone(job.clean())

    def test_clean_cron_string_invalid(self):
        job = self.JobClass()
        job.cron_string = 'not-a-cron-string'
        job.queue = list(settings.RQ_QUEUES)[0]
        job.callable = 'scheduler.tests.test_job'
        with self.assertRaises(ValidationError):
            job.clean_cron_string()

    def test_repeat(self):
        job = self.JobClassFactory(repeat=10)
        entry = _get_job_from_queue(job)
        self.assertEqual(entry.meta['repeat'], 10)

    def test_check_rescheduled_after_execution(self):
        job = self.JobClassFactory()
        queue = job.get_rqueue()
        first_run_id = job.job_id
        entry = queue.fetch_job(first_run_id)
        queue.run_sync(entry)
        job.refresh_from_db()
        self.assertTrue(job.is_scheduled())
        self.assertNotEquals(job.job_id, first_run_id)


class TestSchedulerJob(TestCase):
    def test_scheduler_job_is_running(self):
        # assert job created
        scheduler_cron_job = CronJob.objects.filter(name='Job scheduling jobs').first()
        self.assertIsNotNone(scheduler_cron_job)

        scheduler_cron_job.schedule()  # This should happen in ready
        queue = get_queue('default')
        jobs = queue.scheduled_job_registry.get_job_ids()
        jobs = [queue.fetch_job(job_id) for job_id in jobs]
        scheduler_job = None
        for job in jobs:
            if job.func_name == 'scheduler.apps.reschedule_all_jobs':
                scheduler_job = job
                break
        self.assertIsNotNone(scheduler_job)

        cron_job = CronJobFactory()
        cron_job.unschedule()
        self.assertFalse(cron_job.is_scheduled())
        cron_job.save()
        queue.run_sync(scheduler_job)
        cron_job.refresh_from_db()
        self.assertTrue(cron_job.is_scheduled())

    def test_scheduler_process_is_running(self):
        scheduler = DjangoRQScheduler(interval=1)
        t = scheduler.start()
        assert scheduler.thread == t
        assert scheduler.thread.name == 'Scheduler'
        scheduler.request_stop()
        t.join()
