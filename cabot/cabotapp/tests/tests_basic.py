# -*- coding: utf-8 -*-

import requests
from django.utils import timezone
from django.core.urlresolvers import reverse
from django.test.client import Client
from django.contrib.auth.models import Permission, User
from rest_framework import status, HTTP_HEADER_ENCODING
from rest_framework.test import APITestCase
from rest_framework.reverse import reverse as api_reverse
from twilio import rest
from django.core import mail
from datetime import timedelta, date, datetime
import base64
import json
import os
import socket
from celery.task import task
from cabot.cabotapp import tasks
from mock import Mock, patch

from cabot.cabotapp.models import (
    get_duty_officers, get_all_duty_officers, update_shifts,
    JenkinsStatusCheck, HttpStatusCheck, TCPStatusCheck,
    Service, Schedule, StatusCheckResult)
from cabot.cabotapp.views import StatusCheckReportForm


def get_content(fname):
    path = os.path.join(os.path.dirname(__file__), 'fixtures/%s' % fname)
    with open(path) as f:
        return f.read()


class LocalTestCase(APITestCase):

    def setUp(self):
        requests.get = Mock()
        requests.post = Mock()
        rest.TwilioRestClient = Mock()
        mail.send_mail = Mock()
        self.create_dummy_data()
        super(LocalTestCase, self).setUp()

    def create_dummy_data(self):
        self.username = 'testuser'
        self.password = 'testuserpassword'
        self.user = User.objects.create(username=self.username)
        self.user.set_password(self.password)
        self.user.user_permissions.add(
            Permission.objects.get(codename='add_service'),
            Permission.objects.get(codename='add_httpstatuscheck'),
            Permission.objects.get(codename='add_jenkinsstatuscheck'),
            Permission.objects.get(codename='add_tcpstatuscheck'),
        )
        self.user.save()

        self.jenkins_check = JenkinsStatusCheck.objects.create(
            id=10101,
            name='Jenkins Check',
            created_by=self.user,
            importance=Service.ERROR_STATUS,
            max_queued_build_time=10,
        )
        self.http_check = HttpStatusCheck.objects.create(
            id=10102,
            name='Http Check',
            created_by=self.user,
            importance=Service.CRITICAL_STATUS,
            endpoint='http://arachnys.com',
            timeout=10,
            status_code='200',
            text_match=None,
        )
        self.tcp_check = TCPStatusCheck.objects.create(
            id=10103,
            name='TCP Check',
            created_by=self.user,
            importance=Service.ERROR_STATUS,
            address='github.com',
            port=80,
            timeout=6,
        )

        # Set ical_url for schedule to filename we're using for mock response
        self.schedule = Schedule.objects.create(
            name='Principal',
            ical_url='calendar_response.ics',
        )
        self.secondary_schedule = Schedule.objects.create(
            name='Secondary',
            ical_url='calendar_response_different.ics',
            fallback_officer=self.user,
        )
        self.schedule.save()
        self.secondary_schedule.save()

        self.service = Service.objects.create(
            id=2194,
            name='Service',
        )
        self.service.save()
        self.service.schedules.add(self.schedule)
        self.service.status_checks.add(
            self.jenkins_check,
            self.http_check,
            self.tcp_check)

        # Failing is second most recent
        self.older_result = StatusCheckResult(
            check=self.http_check,
            time=timezone.now() - timedelta(seconds=60),
            time_complete=timezone.now() - timedelta(seconds=59),
            succeeded=False
        )
        self.older_result.save()
        # Passing is most recent
        self.most_recent_result = StatusCheckResult(
            check=self.http_check,
            time=timezone.now() - timedelta(seconds=1),
            time_complete=timezone.now(),
            succeeded=True
        )
        self.most_recent_result.save()
        self.http_check.save()  # Will recalculate status


def fake_jenkins_success(*args, **kwargs):
    resp = Mock()
    resp.raise_for_status.return_value = resp
    resp.json = lambda: json.loads(get_content('jenkins_success.json'))
    resp.status_code = 200
    return resp


def fake_jenkins_response(*args, **kwargs):
    resp = Mock()
    resp.raise_for_status.return_value = resp
    resp.json = lambda: json.loads(get_content('jenkins_response.json'))
    resp.status_code = 400
    return resp


def jenkins_blocked_response(*args, **kwargs):
    resp = Mock()
    resp.json = lambda: json.loads(get_content('jenkins_blocked_response.json'))
    resp.status_code = 200
    return resp


def fake_http_200_response(*args, **kwargs):
    resp = Mock()
    resp.content = get_content('http_response.html')
    resp.status_code = 200
    return resp


def fake_http_404_response(*args, **kwargs):
    resp = Mock()
    resp.content = get_content('http_response.html')
    resp.status_code = 404
    return resp


def fake_tcp_success(*args, **kwargs):
    resp = Mock()
    resp.query.return_value = Mock()
    return resp


def fake_tcp_failure(*args, **kwargs):
    raise socket.timeout


def fake_calendar(*args, **kwargs):
    resp = Mock()
    resp.content = get_content(args)
    resp.status_code = 200
    return resp


@task(ignore_result=True)
def fake_run_status_check(*args, **kwargs):
    resp = Mock()
    return resp


def throws_timeout(*args, **kwargs):
    raise requests.RequestException(u'something bad happened')


class TestCheckRun(LocalTestCase):

    def test_calculate_service_status(self):
        self.assertEqual(self.jenkins_check.calculated_status,
                         Service.CALCULATED_PASSING_STATUS)
        self.assertEqual(self.http_check.calculated_status,
                         Service.CALCULATED_PASSING_STATUS)
        self.assertEqual(self.tcp_check.calculated_status,
                         Service.CALCULATED_PASSING_STATUS)
        self.service.update_status()
        self.assertEqual(self.service.overall_status, Service.PASSING_STATUS)

        # Now two most recent are failing
        self.most_recent_result.succeeded = False
        self.most_recent_result.save()
        self.http_check.last_run = timezone.now()
        self.http_check.save()
        self.assertEqual(self.http_check.calculated_status,
                         Service.CALCULATED_FAILING_STATUS)
        self.service.update_status()
        self.assertEqual(self.service.overall_status, Service.CRITICAL_STATUS)

        # Will fail even if second one is working
        self.older_result.succeeded = True
        self.older_result.save()
        self.http_check.save()
        self.assertEqual(self.http_check.calculated_status,
                         Service.CALCULATED_FAILING_STATUS)
        self.service.update_status()
        self.assertEqual(self.service.overall_status, Service.CRITICAL_STATUS)

        # Changing the number of retries will change it up
        self.http_check.retries = 1
        self.http_check.save()
        self.assertEqual(self.http_check.calculated_status,
                         Service.CALCULATED_PASSING_STATUS)
        self.service.update_status()
        self.assertEqual(self.service.overall_status, Service.PASSING_STATUS)

    @patch('cabot.cabotapp.jenkins.requests.get', fake_jenkins_success)
    def test_jenkins_success(self):
        checkresults = self.jenkins_check.statuscheckresult_set.all()
        self.assertEqual(len(checkresults), 0)
        self.jenkins_check.run()
        checkresults = self.jenkins_check.statuscheckresult_set.all()
        self.assertEqual(len(checkresults), 1)
        self.assertTrue(self.jenkins_check.last_result().succeeded)

    @patch('cabot.cabotapp.jenkins.requests.get', fake_jenkins_response)
    def test_jenkins_run(self):
        checkresults = self.jenkins_check.statuscheckresult_set.all()
        self.assertEqual(len(checkresults), 0)
        self.jenkins_check.run()
        checkresults = self.jenkins_check.statuscheckresult_set.all()
        self.assertEqual(len(checkresults), 1)
        self.assertFalse(self.jenkins_check.last_result().succeeded)

    @patch('cabot.cabotapp.jenkins.requests.get', jenkins_blocked_response)
    def test_jenkins_blocked_build(self):
        checkresults = self.jenkins_check.statuscheckresult_set.all()
        self.assertEqual(len(checkresults), 0)
        self.jenkins_check.run()
        checkresults = self.jenkins_check.statuscheckresult_set.all()
        self.assertEqual(len(checkresults), 1)
        self.assertFalse(self.jenkins_check.last_result().succeeded)

    @patch('cabot.cabotapp.models.requests.get', throws_timeout)
    def test_timeout_handling_in_jenkins(self):
        checkresults = self.jenkins_check.statuscheckresult_set.all()
        self.assertEqual(len(checkresults), 0)
        self.jenkins_check.run()
        checkresults = self.jenkins_check.statuscheckresult_set.all()
        self.assertEqual(len(checkresults), 1)
        self.assertFalse(self.jenkins_check.last_result().succeeded)
        self.assertIn(u'Error fetching from Jenkins - something bad happened',
                      self.jenkins_check.last_result().error)

    @patch('cabot.cabotapp.models.requests.request', fake_http_200_response)
    def test_http_run(self):
        checkresults = self.http_check.statuscheckresult_set.all()
        self.assertEqual(len(checkresults), 2)
        self.http_check.run()
        checkresults = self.http_check.statuscheckresult_set.all()
        self.assertEqual(len(checkresults), 3)
        self.assertTrue(self.http_check.last_result().succeeded)
        self.assertEqual(self.http_check.calculated_status,
                         Service.CALCULATED_PASSING_STATUS)
        self.http_check.text_match = u'blah blah'
        self.http_check.save()
        self.http_check.run()
        self.assertFalse(self.http_check.last_result().succeeded)
        self.assertEqual(self.http_check.calculated_status,
                         Service.CALCULATED_FAILING_STATUS)
        # Unicode
        self.http_check.text_match = u'This is not in the http response!!'
        self.http_check.save()
        self.http_check.run()
        self.assertFalse(self.http_check.last_result().succeeded)
        self.assertEqual(self.http_check.calculated_status,
                         Service.CALCULATED_FAILING_STATUS)

    @patch('cabot.cabotapp.models.requests.request', throws_timeout)
    def test_timeout_handling_in_http(self):
        checkresults = self.http_check.statuscheckresult_set.all()
        self.assertEqual(len(checkresults), 2)
        self.http_check.run()
        checkresults = self.http_check.statuscheckresult_set.all()
        self.assertEqual(len(checkresults), 3)
        self.assertFalse(self.http_check.last_result().succeeded)
        self.assertIn(u'Request error occurred: something bad happened',
                      self.http_check.last_result().error)

    @patch('cabot.cabotapp.models.requests.request', fake_http_404_response)
    def test_http_run_bad_resp(self):
        checkresults = self.http_check.statuscheckresult_set.all()
        self.assertEqual(len(checkresults), 2)
        self.http_check.run()
        checkresults = self.http_check.statuscheckresult_set.all()
        self.assertEqual(len(checkresults), 3)
        self.assertFalse(self.http_check.last_result().succeeded)
        self.assertEqual(self.http_check.calculated_status,
                         Service.CALCULATED_FAILING_STATUS)

    @patch('cabot.cabotapp.models.socket.create_connection', fake_tcp_success)
    def test_tcp_success(self):
        checkresults = self.tcp_check.statuscheckresult_set.all()
        self.assertEqual(len(checkresults), 0)
        self.tcp_check.run()
        checkresults = self.tcp_check.statuscheckresult_set.all()
        self.assertEqual(len(checkresults), 1)
        self.assertTrue(self.tcp_check.last_result().succeeded)

    @patch('cabot.cabotapp.models.socket.create_connection', fake_tcp_failure)
    def test_tcp_failure(self):
        checkresults = self.tcp_check.statuscheckresult_set.all()
        self.assertEqual(len(checkresults), 0)
        self.tcp_check.run()
        checkresults = self.tcp_check.statuscheckresult_set.all()
        self.assertEqual(len(checkresults), 1)
        self.assertFalse(self.tcp_check.last_result().succeeded)
        self.assertFalse(self.tcp_check.last_result().error, 'timed out')


class TestStatusCheck(LocalTestCase):

    def test_duplicate_statuscheck(self):
        """
        Test that duplicating a statuscheck works and creates a check
        with the name we expect.
        """
        http_checks = HttpStatusCheck.objects.filter(polymorphic_ctype__model='httpstatuscheck')
        self.assertEqual(len(http_checks), 1)

        self.http_check.duplicate()

        http_checks = HttpStatusCheck.objects.filter(polymorphic_ctype__model='httpstatuscheck')
        self.assertEqual(len(http_checks), 2)

        new = http_checks.filter(name__icontains='Copy of')[0]
        old = http_checks.exclude(name__icontains='Copy of')[0]

        # New check should be the same as the old check except for the name
        self.assertEqual(new.name, 'Copy of {}'.format(old.name))
        self.assertEqual(new.endpoint, old.endpoint)
        self.assertEqual(new.status_code, old.status_code)

    @patch('cabot.cabotapp.tasks.run_status_check', fake_run_status_check)
    def test_run_all(self):
        tasks.run_all_checks()


class TestWebInterface(LocalTestCase):

    def setUp(self):
        super(TestWebInterface, self).setUp()
        self.client = Client()

    def test_set_recovery_instructions(self):
        # Get service page - will get 200 from login page
        resp = self.client.get(reverse('update-service', kwargs={'pk': self.service.id}), follow=True)
        self.assertEqual(resp.status_code, 200)

        # Log in
        self.client.login(username=self.username, password=self.password)
        resp = self.client.get(reverse('update-service', kwargs={'pk': self.service.id}))
        self.assertEqual(resp.status_code, 200)
        self.assertNotIn('username', resp.content)

        snippet_link = 'https://sub.hackpad.com/wiki-7YaNlsC11bB.js'
        self.assertEqual(self.service.hackpad_id, None)
        resp = self.client.post(
            reverse('update-service', kwargs={'pk': self.service.id}),
            data={
                'name': self.service.name,
                'hackpad_id': snippet_link,
            },
            follow=True,
        )
        self.assertEqual(resp.status_code, 200)
        reloaded = Service.objects.get(id=self.service.id)
        self.assertEqual(reloaded.hackpad_id, snippet_link)
        # Now one on the blacklist
        blacklist_link = 'https://unapproved_link.domain.com/wiki-7YaNlsC11bB.js'
        resp = self.client.post(
            reverse('update-service', kwargs={'pk': self.service.id}),
            data={
                'name': self.service.name,
                'hackpad_id': blacklist_link,
            },
            follow=True,
        )
        self.assertEqual(resp.status_code, 200)
        self.assertIn('valid JS snippet link', resp.content)
        reloaded = Service.objects.get(id=self.service.id)
        # Still the same
        self.assertEqual(reloaded.hackpad_id, snippet_link)

    def test_checks_report(self):
        form = StatusCheckReportForm({
            'service': self.service.id,
            'checks': [self.http_check.id],
            'date_from': date.today() - timedelta(days=1),
            'date_to': date.today(),
        })
        self.assertTrue(form.is_valid())
        checks = form.get_report()
        self.assertEqual(len(checks), 1)
        check = checks[0]
        self.assertEqual(len(check.problems), 1)
        self.assertEqual(check.success_rate, 50)


class TestAPI(LocalTestCase):
    def setUp(self):
        super(TestAPI, self).setUp()

        self.basic_auth = 'Basic {}'.format(
            base64.b64encode(
                '{}:{}'.format(self.username, self.password).encode(HTTP_HEADER_ENCODING)
            ).decode(HTTP_HEADER_ENCODING)
        )

        self.start_data = {
            'service': [
                {
                    'name': u'Service',
                    'users_to_notify': [],
                    'alerts_enabled': True,
                    'status_checks': [10101, 10102, 10103],
                    'alerts': [],
                    'hackpad_id': None,
                    'id': 2194,
                    'url': u''
                },
            ],
            'statuscheck': [
                {
                    'name': u'Jenkins Check',
                    'active': True,
                    'importance': u'ERROR',
                    'frequency': 5,
                    'retries': 0,
                    'id': 10101
                },
                {
                    'name': u'Http Check',
                    'active': True,
                    'importance': u'CRITICAL',
                    'frequency': 5,
                    'retries': 0,
                    'id': 10102
                },
                {
                    'name': u'TCP Check',
                    'active': True,
                    'importance': u'ERROR',
                    'frequency': 5,
                    'retries': 0,
                    'id': 10103
                },
            ],
            'jenkinsstatuscheck': [
                {
                    'name': u'Jenkins Check',
                    'active': True,
                    'importance': u'ERROR',
                    'frequency': 5,
                    'retries': 0,
                    'max_queued_build_time': 10,
                    'id': 10101
                },
            ],
            'httpstatuscheck': [
                {
                    'name': u'Http Check',
                    'active': True,
                    'importance': u'CRITICAL',
                    'frequency': 5,
                    'retries': 0,
                    'endpoint': u'http://arachnys.com',
                    'username': None,
                    'password': None,
                    'text_match': None,
                    'status_code': u'200',
                    'timeout': 10,
                    'verify_ssl_certificate': True,
                    'id': 10102
                },
            ],
            'tcpstatuscheck': [
                {
                    'name': u'TCP Check',
                    'active': True,
                    'importance': u'ERROR',
                    'frequency': 5,
                    'retries': 0,
                    'address': 'github.com',
                    'port': 80,
                    'timeout': 6,
                    'id': 10103
                },
            ],
        }
        self.post_data = {
            'service': [
                {
                    'name': u'posted service',
                    'users_to_notify': [],
                    'alerts_enabled': True,
                    'status_checks': [],
                    'alerts': [],
                    'hackpad_id': None,
                    'id': 2194,
                    'url': u'',
                },
            ],
            'jenkinsstatuscheck': [
                {
                    'name': u'posted jenkins check',
                    'active': True,
                    'importance': u'CRITICAL',
                    'frequency': 5,
                    'retries': 0,
                    'max_queued_build_time': 37,
                    'id': 10101
                },
            ],
            'httpstatuscheck': [
                {
                    'name': u'posted http check',
                    'active': True,
                    'importance': u'ERROR',
                    'frequency': 5,
                    'retries': 0,
                    'endpoint': u'http://arachnys.com/post_tests',
                    'username': None,
                    'password': None,
                    'text_match': u'text',
                    'status_code': u'201',
                    'timeout': 30,
                    'verify_ssl_certificate': True,
                    'id': 10102
                },
            ],
            'tcpstatuscheck': [
                {
                    'name': u'posted tcp check',
                    'active': True,
                    'importance': u'ERROR',
                    'frequency': 5,
                    'retries': 0,
                    'address': 'github.com',
                    'port': 80,
                    'timeout': 6,
                    'id': 10103
                },
            ],
        }

    def test_auth_failure(self):
        response = self.client.get(api_reverse('statuscheck-list'))
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

    def normalize_dict(self, operand):
        for key, val in operand.items():
            if isinstance(val, list):
                operand[key] = sorted(val)
        return operand

    def test_gets(self):
        for model, items in self.start_data.items():
            response = self.client.get(api_reverse('{}-list'.format(model)),
                                       format='json', HTTP_AUTHORIZATION=self.basic_auth)
            self.assertEqual(response.status_code, status.HTTP_200_OK)
            self.assertEqual(len(response.data), len(items))
            for response_item, item in zip(response.data, items):
                self.assertEqual(self.normalize_dict(response_item), item)
            for item in items:
                response = self.client.get(api_reverse('{}-detail'.format(model), args=[item['id']]),
                                           format='json', HTTP_AUTHORIZATION=self.basic_auth)
                self.assertEqual(response.status_code, status.HTTP_200_OK)
                self.assertEqual(self.normalize_dict(response.data), item)

    def test_posts(self):
        for model, items in self.post_data.items():
            for item in items:
                # hackpad_id and other null text fields omitted on create
                # for now due to rest_framework bug:
                # https://github.com/tomchristie/django-rest-framework/issues/1879
                # Update: This has been fixed in master:
                # https://github.com/tomchristie/django-rest-framework/pull/1834
                for field in ('hackpad_id', 'username', 'password'):
                    if field in item:
                        del item[field]
                create_response = self.client.post(api_reverse('{}-list'.format(model)),
                                                   format='json', data=item, HTTP_AUTHORIZATION=self.basic_auth)
                self.assertEqual(create_response.status_code, status.HTTP_201_CREATED)
                self.assertTrue('id' in create_response.data)
                item['id'] = create_response.data['id']
                for field in ('hackpad_id', 'username', 'password'):  # See comment above
                    if field in create_response.data:
                        item[field] = None
                self.assertEqual(self.normalize_dict(create_response.data), item)
                get_response = self.client.get(api_reverse('{}-detail'.format(model), args=[item['id']]),
                                               format='json', HTTP_AUTHORIZATION=self.basic_auth)
                self.assertEqual(self.normalize_dict(get_response.data), item)


class TestAPIFiltering(LocalTestCase):
    def setUp(self):
        super(TestAPIFiltering, self).setUp()

        self.expected_filter_result = JenkinsStatusCheck.objects.create(
            name='Filter test 1',
            retries=True,
            importance=Service.CRITICAL_STATUS,
        )
        JenkinsStatusCheck.objects.create(
            name='Filter test 2',
            retries=True,
            importance=Service.WARNING_STATUS,
        )
        JenkinsStatusCheck.objects.create(
            name='Filter test 3',
            retries=False,
            importance=Service.CRITICAL_STATUS,
        )

        self.expected_sort_names = [u'Filter test 1', u'Filter test 2', u'Filter test 3', u'Jenkins Check']

        self.basic_auth = 'Basic {}'.format(
            base64.b64encode(
                '{}:{}'.format(self.username, self.password)
                       .encode(HTTP_HEADER_ENCODING)
            ).decode(HTTP_HEADER_ENCODING)
        )

    def test_query(self):
        response = self.client.get(
            '{}?retries=1&importance=CRITICAL'.format(
                api_reverse('jenkinsstatuscheck-list')
            ),
            format='json',
            HTTP_AUTHORIZATION=self.basic_auth
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data), 1)
        self.assertEqual(
            response.data[0]['id'],
            self.expected_filter_result.id
        )

    def test_positive_sort(self):
        response = self.client.get(
            '{}?ordering=name'.format(
                api_reverse('jenkinsstatuscheck-list')
            ),
            format='json',
            HTTP_AUTHORIZATION=self.basic_auth
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(
            [item['name'] for item in response.data],
            self.expected_sort_names
        )

    def test_negative_sort(self):
        response = self.client.get(
            '{}?ordering=-name'.format(
                api_reverse('jenkinsstatuscheck-list')
            ),
            format='json',
            HTTP_AUTHORIZATION=self.basic_auth
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(
            [item['name'] for item in response.data],
            self.expected_sort_names[::-1]
        )


class TestSchedules(LocalTestCase):
    def setUp(self):
        super(TestSchedules, self).setUp()
        self.create_fake_users(['dolores@affirm.com', 'bernard@affirm.com', 'teddy@affirm.com',
                                'maeve@affirm.com', 'hector@affirm.com', 'armistice@affirm.com',
                                'longnamelongnamelongnamelongname@affirm.com', 'shortname@affirm.com'])

    def create_fake_users(self, usernames):
        """Create fake Users with the listed usernames"""
        for user in usernames:
            User.objects.create(
                username=user[:30],
                password='fakepassword',
                email=user,
                is_active=True,
            )

    @patch('cabot.cabotapp.models.requests.get', fake_calendar)
    def test_single_schedule(self):
        """
        Make sure the correct person is marked as a duty officer
        if there's a single calendar
        """
        # initial user plus new 8
        self.assertEqual(len(User.objects.all()), 9)

        update_shifts(self.schedule)

        officers = get_duty_officers(self.schedule, at_time=datetime(2016, 11, 6, 0, 0, 0))
        usernames = [str(user.username) for user in officers]
        self.assertEqual(usernames, ['dolores@affirm.com'])

        officers = get_duty_officers(self.schedule, at_time=datetime(2016, 11, 8, 0, 0, 0))
        usernames = [str(user.username) for user in officers]
        self.assertEqual(usernames, ['teddy@affirm.com'])

        officers = get_duty_officers(self.schedule, at_time=datetime(2016, 11, 8, 10, 0, 0))
        usernames = [str(user.username) for user in officers]
        self.assertEqual(usernames, ['teddy@affirm.com'])

    @patch('cabot.cabotapp.models.requests.get', fake_calendar)
    def test_update_schedule_twice(self):
        """Make sure nothing changes if you update twice"""
        for _ in range(2):
            update_shifts(self.schedule)
            officers = get_duty_officers(self.schedule, at_time=datetime(2016, 11, 6, 0, 0, 0))
            usernames = [str(user.username) for user in officers]
            self.assertEqual(usernames, ['dolores@affirm.com'])

    @patch('cabot.cabotapp.models.requests.get', fake_calendar)
    def test_multiple_schedules(self):
        """
        Add a second calendar and make sure the correct duty officers are marked
        for each calendar
        """
        self.assertEqual(len(User.objects.all()), 9)

        update_shifts(self.secondary_schedule)
        update_shifts(self.schedule)

        officers = get_duty_officers(self.secondary_schedule, at_time=datetime(2016, 11, 6, 0, 0, 0))
        usernames = [str(user.username) for user in officers]
        self.assertEqual(usernames, ['maeve@affirm.com'])

        old_officers = get_duty_officers(self.schedule, at_time=datetime(2016, 11, 6, 0, 0, 0))
        old_usernames = [user.username for user in old_officers]
        self.assertEqual(old_usernames, ['dolores@affirm.com'])

    @patch('cabot.cabotapp.models.requests.get', fake_calendar)
    def test_get_all_duty_officers(self):
        """
        Make sure get_all_duty_officers works with multiple calendars
        """
        self.assertEqual(len(User.objects.all()), 9)

        update_shifts(self.schedule)
        update_shifts(self.secondary_schedule)

        officers_dict = get_all_duty_officers(at_time=datetime(2016, 11, 6, 0, 0, 0))
        officers = []
        for item in officers_dict.iteritems():
            officers.append(item)

        self.assertEqual(len(officers), 2)

        officer_schedule = [(officers[0][0].username, officers[0][1][0].name),
                            (officers[1][0].username, officers[1][1][0].name)]
        self.assertIn(('dolores@affirm.com', 'Principal'), officer_schedule)
        self.assertIn(('maeve@affirm.com', 'Secondary'), officer_schedule)

    @patch('cabot.cabotapp.models.requests.get', fake_calendar)
    def test_calendar_update_remove_oncall(self):
        """
        Test that an oncall officer gets removed if they aren't on the schedule
        """
        update_shifts(self.schedule)

        officers = get_duty_officers(self.schedule, at_time=datetime(2016, 11, 8, 10, 0, 0))
        usernames = [str(user.username) for user in officers]
        self.assertEqual(usernames, ['teddy@affirm.com'])

        # Change the schedule
        self.schedule.ical_url = 'calendar_response_different.ics'
        self.schedule.save()
        update_shifts(self.schedule)

        officers = get_duty_officers(self.schedule, at_time=datetime(2016, 11, 8, 10, 0, 0))
        usernames = [str(user.username) for user in officers]
        self.assertEqual(usernames, ['hector@affirm.com'])

    @patch('cabot.cabotapp.models.requests.get', fake_calendar)
    def test_calendar_long_name(self):
        """
        Test that we can sync oncall schedules for users with emails > 30 characters
        """
        self.schedule.ical_url = 'calendar_response_long_name.ics'
        self.schedule.save()
        update_shifts(self.schedule)

        officers = get_duty_officers(self.schedule, at_time=datetime(2016, 11, 7, 10, 0, 0))
        emails = [str(user.email) for user in officers]
        self.assertEqual(emails, ['longnamelongnamelongnamelongname@affirm.com'])
