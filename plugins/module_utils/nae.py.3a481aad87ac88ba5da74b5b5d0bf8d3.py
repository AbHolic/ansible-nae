# -*- coding: utf-8 -*-

# This code is part of Ansible, but is an independent component

# This particular file snippet, and this file snippet only, is BSD licensed.
# Modules you write using this snippet, which is embedded dynamically by Ansible
# still belong to the author of the module, and may assign their own license
# to the complete work.


# All rights reserved.

# Redistribution and use in source and binary forms, with or without modification,
# are permitted provided that the following conditions are met:
#
#    * Redistributions of source code must retain the above copyright
#      notice, this list of conditions and the following disclaimer.
#    * Redistributions in binary form must reproduce the above copyright notice,
#      this list of conditions and the following disclaimer in the documentation
#      and/or other materials provided with the distribution.
#
# THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS "AS IS" AND
# ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT LIMITED TO, THE IMPLIED
# WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A PARTICULAR PURPOSE ARE DISCLAIMED.
# IN NO EVENT SHALL THE COPYRIGHT HOLDER OR CONTRIBUTORS BE LIABLE FOR ANY DIRECT, INDIRECT,
# INCIDENTAL, SPECIAL, EXEMPLARY, OR CONSEQUENTIAL DAMAGES (INCLUDING, BUT NOT LIMITED TO,
# PROCUREMENT OF SUBSTITUTE GOODS OR SERVICES; LOSS OF USE, DATA, OR PROFITS; OR BUSINESS
# INTERRUPTION) HOWEVER CAUSED AND ON ANY THEORY OF LIABILITY, WHETHER IN CONTRACT, STRICT
# LIABILITY, OR TORT (INCLUDING NEGLIGENCE OR OTHERWISE) ARISING IN ANY WAY OUT OF THE
# USE OF THIS SOFTWARE, EVEN IF ADVISED OF THE POSSIBILITY OF SUCH DAMAGE.

from __future__ import (absolute_import, division, print_function)
__metaclass__ = type

from requests_toolbelt.multipart.encoder import MultipartEncoder
from datetime import datetime
import base64
import requests
import csv
import json
import os
import time
import gzip
import filelock
import pathlib
import hashlib
from copy import deepcopy
from ansible.module_utils.basic import AnsibleModule
from ansible.module_utils.parsing.convert_bool import boolean
from ansible.module_utils.urls import fetch_url
from ansible.module_utils._text import to_bytes, to_native
from jsonpath_ng import jsonpath, parse


def nae_argument_spec():
    return dict(
        host=dict(type='str', required=True, aliases=['hostname']),
        port=dict(type='int', required=False, default=443),
        username=dict(type='str', default='admin', aliases=['user']),
        password=dict(type='str', no_log=True),
    )


class NAEModule(object):
    def __init__(self, module):
        self.module = module
        self.resp = {}
        self.params = module.params
        self.result = dict(changed=False)
        self.files = {}
        self.assuranceGroups = []
        self.session_cookie = ""
        self.error = dict(code=None, text=None)
        self.version = ""
        self.http_headers = {
            'Accept': '*/*',
            'Accept-Encoding': 'gzip, deflate, br',
            'Accept-Language': 'en-GB,en-US;q=0.9,en;q=0.8,it;q=0.7',
            'Sec-Fetch-Mode': 'cors',
            'Sec-Fetch-Site': 'same-origin',
            'Host': self.params.get('host'),
            'Content-Type': 'application/json;charset=utf-8',
            'Connection': 'keep-alive'}
        self.login()

    def login(self):
        url = 'https://%(host)s:%(port)s/nae/api/v1/whoami' % self.params
        resp, auth = fetch_url(self.module, url,
                               data=None,
                               method='GET')

        if auth.get('status') != 200:
            if('filename' in self.params):
                self.params['file'] = self.params['filename']
                del self.params['filename']
            self.response = auth.get('msg')
            self.status = auth.get('status')
            try:
                self.module.fail_json(msg=self.response, **self.result)
            except KeyError:
                # Connection error
                self.fail_json(
                    msg='Connection failed for %(url)s. %(msg)s' %
                    auth, **self.result)

        url = 'https://%(host)s:%(port)s/nae/api/v1/login' % self.params
        user_credentials = json.dumps({"username": self.params.get(
            'username'), "password": self.params.get('password'), "domain": 'Local'})
        self.http_headers['Cookie'] = resp.headers.get('Set-Cookie')
        self.session_cookie = resp.headers.get('Set-Cookie')
        self.http_headers['X-NAE-LOGIN-OTP'] = resp.headers.get(
            'X-NAE-LOGIN-OTP')
        resp, auth = fetch_url(self.module, url,
                               headers=self.http_headers,
                               data=user_credentials,
                               method='POST')

        if auth.get('status') != 200:
            if('filename' in self.params):
                self.params['file'] = self.params['filename']
                del self.params['filename']
            self.module.exit_json(
                msg=json.loads(
                    auth.get('body'))['messages'][0]['message'],
                **self.result)

        self.http_headers['X-NAE-CSRF-TOKEN'] = resp.headers['X-NAE-CSRF-TOKEN']

        # # Update with the authenticated Cookie
        self.http_headers['Cookie'] = resp.headers.get('Set-Cookie')

        # Remove the LOGIN-OTP from header, it is only needed at the beginning
        self.http_headers.pop('X-NAE-LOGIN-OTP', None)
        url = 'https://%(host)s:%(port)s/nae/api/v1/event-services/candid-version' % self.params
        resp, auth = fetch_url(
            self.module, url, headers=self.http_headers, data=None, method='GET')
        if auth.get('status') != 200:
            if('filename' in self.params):
                self.params['file'] = self.params['filename']
                del self.params['filename']
            self.response = auth.get('msg')
            self.status = auth.get('status')
            try:
                self.module.fail_json(msg=self.response, **self.result)
            except KeyError:
                # Connection error
                self.fail_json(
                    msg='Connection failed for %(url)s. %(msg)s' %
                    auth, **self.result)
        self.version = json.loads(
            resp.read())['value']['data']['candid_version']
        # self.result['response'] = data

    def get_logout_lock(self):
        # This lock has been introduced because logout and file upload cannot be
        # done in parallel. This is because logout incorrectly aborts all file
        # uploads by a user (not just that session). So, this lock must be
        # acquired for logout and file upload.
        lock_filename = "logout.lock"
        try:
            pathlib.Path(lock_filename).touch(exist_ok=False)
        except OSError:
            pass
        return filelock.FileLock(lock_filename)

    def get_all_assurance_groups(self):
        url = 'https://%(host)s:%(port)s/nae/api/v1/config-services/assured-networks/aci-fabric/' % self.params
        resp, auth = fetch_url(self.module, url,
                               headers=self.http_headers,
                               data=None,
                               method='GET')

        if auth.get('status') != 200:
            if('filename' in self.params):
                self.params['file'] = self.params['filename']
                del self.params['filename']
            self.module.exit_json(
                msg=json.loads(
                    auth.get('body'))['messages'][0]['message'],
                **self.result)

        if resp.headers['Content-Encoding'] == "gzip":
            r = gzip.decompress(resp.read())
            self.assuranceGroups = json.loads(r.decode())['value']['data']
            return
        self.assuranceGroups = json.loads(resp.read())['value']['data']

    def get_assurance_group(self, name):
        self.get_all_assurance_groups()
        for ag in self.assuranceGroups:
            if ag['unique_name'] == name:
                return ag
        return None

    def deleteAG(self):
        self.params['uuid'] = str(
            self.get_assurance_group(
                self.params.get('name'))['uuid'])
        url = 'https://%(host)s:%(port)s/nae/api/v1/config-services/assured-networks/aci-fabric/%(uuid)s' % self.params
        resp, auth = fetch_url(self.module, url,
                               headers=self.http_headers,
                               data=None,
                               method='DELETE')
        if auth.get('status') != 200:
            if('filename' in self.params):
                self.params['file'] = self.params['filename']
                del self.params['filename']
            self.response = auth.get('msg')
            self.status = auth.get('status')
            try:
                self.module.fail_json(msg=self.response, **self.result)
            except KeyError:
                # Connection error
                self.fail_json(
                    msg='Connection failed for %(url)s. %(msg)s' %
                    auth, **self.result)
        if json.loads(resp.read())['success'] is True:
            self.result['Result'] = 'Assurance Group "%(name)s" deleted successfully' % self.params

    def newOnlineAG(self):
        # This method creates a new Offline Assurance Group, you only need to
        # pass the AG Name.

        url = 'https://%(host)s:%(port)s/nae/api/v1/config-services/assured-networks/aci-fabric/' % self.params

        form = '''{
          "analysis_id": "",
          "display_name": "",
          "description": "",
          "interval": 900,
          "password": "''' + str(self.params.get('apic_password')) + '''",
          "operational_mode": "ONLINE",
          "status": "STOPPED",
          "active": true,
          "unique_name": "''' + str(self.params.get('name')) + '''",
          "assured_network_type": "",
          "apic_hostnames": [ "''' + str(self.params.get('apic_hostnames')) + '''" ],
          "username": "''' + str(self.params.get('apic_username')) + '''",
          "analysis_timeout_in_secs": 3600,
          "apic_configuration_export_policy": {
            "apic_configuration_export_policy_enabled": "''' + str(self.params.get('export_apic_policy')) + '''",
            "export_format": "JSON",
            "export_policy_name": "''' + str(self.params.get('name')) + '''"
          },
          "nat_configuration": null,
          "assured_fabric_type": null,
          "analysis_schedule_id": ""}'''

        resp, auth = fetch_url(self.module, url,
                               headers=self.http_headers,
                               data=form,
                               method='POST')

        if auth.get('status') != 201:
            if('filename' in self.params):
                self.params['file'] = self.params['filename']
                del self.params['filename']
            self.response = auth.get('msg')
            self.status = auth.get('status')
            try:
                self.module.fail_json(
                    msg=str(self.response) + str(self.status), **self.result)
            except KeyError:
                # Connection error
                self.fail_json(
                    msg='Connection failed for %(url)s. %(msg)s' %
                    auth, **self.result)
        self.result['Result'] = 'Successfully created Assurance Group "%(name)s"' % self.params

    def newOfflineAG(self):
        # This method creates a new Offline Assurance Group, you only need to
        # pass the AG Name.

        url = 'https://%(host)s:%(port)s/nae/api/v1/config-services/assured-networks/aci-fabric/' % self.params

        form = '''{
          "analysis_id": "",
          "display_name": "",
          "description": "",
          "operational_mode": "OFFLINE",
          "status": "STOPPED",
          "active": true,
          "unique_name": "''' + str(self.params.get('name')) + '''",
          "assured_network_type": "",
          "analysis_timeout_in_secs": 3600,
          "apic_configuration_export_policy": {
            "apic_configuration_export_policy_enabled": false,
            "export_format": "XML",
            "export_policy_name": ""
          },
          "analysis_schedule_id": ""}'''

        resp, auth = fetch_url(self.module, url,
                               headers=self.http_headers,
                               data=form,
                               method='POST')

        if auth.get('status') != 201:
            if('filename' in self.params):
                self.params['file'] = self.params['filename']
                del self.params['filename']
            self.response = auth.get('msg')
            self.status = auth.get('status')
            try:
                self.module.fail_json(
                    msg=str(self.response) + str(self.status), **self.result)
            except KeyError:
                # Connection error
                self.fail_json(
                    msg='Connection failed for %(url)s. %(msg)s' %
                    auth, **self.result)
        self.result['Result'] = 'Successfully created Assurance Group "%(name)s"' % self.params

    def get_pre_change_analyses(self):
        self.params['fabric_id'] = str(
            self.get_assurance_group(
                self.params.get('ag_name'))['uuid'])
        url = 'https://%(host)s:%(port)s/nae/api/v1/config-services/prechange-analysis?fabric_id=%(fabric_id)s' % self.params
        resp, auth = fetch_url(self.module, url,
                               headers=self.http_headers,
                               data=None,
                               method='GET')
        # self.result['resp'] = resp.headers.get('Set-Cookie')
        # self.module.fail_json(msg="err", **self.result)
        if auth.get('status') != 200:
            if('filename' in self.params):
                self.params['file'] = self.params['filename']
                del self.params['filename']
            self.module.exit_json(
                msg=json.loads(
                    auth.get('body'))['messages'][0]['message'],
                **self.result)

        if resp.headers['Content-Encoding'] == "gzip":
            r = gzip.decompress(resp.read())
            return json.loads(r.decode())['value']['data']
        return json.loads(resp.read())['value']['data']

    def show_pre_change_analyses(self):
        result = self.get_pre_change_analyses()
        for x in result:
            if 'description' not in x:
                x['description'] = ""
            if 'job_id' in x:
                del x['job_id']
            if 'fabric_uuid' in x:
                del x['fabric_uuid']
            if 'base_epoch_id' in x:
                del x['base_epoch_id']
            if 'base_epoch_collection_time_rfc3339':
                del x['base_epoch_collection_time_rfc3339']
            if 'pre_change_epoch_uuid' in x:
                del x['pre_change_epoch_uuid']
            if 'analysis_schedule_id' in x:
                del x['analysis_schedule_id']
            if 'epoch_delta_job_id' in x:
                del x['epoch_delta_job_id']
            if 'enable_download' in x:
                del x['enable_download']
            if 'allow_unsupported_object_modification' in x:
                del x['allow_unsupported_object_modification']
            if 'changes' in x:
                del x['changes']
            if 'change_type' in x:
                del x['change_type']
            if 'uploaded_file_name' in x:
                del x['uploaded_file_name']
            if 'stop_analysis' in x:
                del x['stop_analysis']
            if 'submitter_domain' in x:
                del x['submitter_domain']

            m = str(x['base_epoch_collection_timestamp'])[:10]
            dt_object = datetime.fromtimestamp(int(m))
            x['base_epoch_collection_timestamp'] = dt_object

            m = str(x['analysis_submission_time'])[:10]
            dt_object = datetime.fromtimestamp(int(m))
            x['analysis_submission_time'] = dt_object
        self.result['Analyses'] = result
        return result

    def is_json(self, myjson):
        try:
            json_object = json.loads(myjson)
        except ValueError as e:
            return False
        return True

    def get_pre_change_analysis(self):
        ret = self.get_pre_change_analyses()
        # self.result['ret'] = ret
        for a in ret:
            if a['name'] == self.params.get('name'):
                # self.result['analysis'] = a
                return a
        return None

    def get_pre_change_result(self):
        if self.get_assurance_group(self.params.get('ag_name')) is None:
            self.module.exit_json(
                msg='No such Assurance Group exists on this fabric.')
        self.params['fabric_id'] = str(
            self.get_assurance_group(
                self.params.get('ag_name'))['uuid'])
        if self.get_pre_change_analysis() is None:
            self.module.fail_json(
                msg='No such Pre-Change Job exists.',
                **self.result)
        if self.params['verify']:
            status = None
            while status != "COMPLETED":
                try:
                    status = str(
                        self.get_pre_change_analysis()['analysis_status'])
                    if status == "COMPLETED":
                        break
                except BaseException:
                    pass
                time.sleep(30)
        else:
            job_is_done = str(
                self.get_pre_change_analysis()['analysis_status'])
            if job_is_done != "COMPLETED":
                self.module.exit_json(
                    msg='Pre-Change Job has not yet completed.', **self.result)
        self.params['epoch_delta_job_id'] = str(
            self.get_pre_change_analysis()['epoch_delta_job_id'])
        url = 'https://%(host)s:%(port)s/nae/api/v1/epoch-delta-services/assured-networks/%(fabric_id)s/job/%(epoch_delta_job_id)s/health/view/aggregate-table?category=ADC,CHANGE_ANALYSIS,TENANT_ENDPOINT,TENANT_FORWARDING,TENANT_SECURITY,RESOURCE_UTILIZATION,SYSTEM,COMPLIANCE&epoch_status=EPOCH2_ONLY&severity=EVENT_SEVERITY_CRITICAL,EVENT_SEVERITY_MAJOR,EVENT_SEVERITY_MINOR,EVENT_SEVERITY_WARNING,EVENT_SEVERITY_INFO' % self.params
        resp, auth = fetch_url(self.module, url,
                               headers=self.http_headers,
                               data=None,
                               method='GET')
        if auth.get('status') != 200:
            self.module.exit_json(
                msg=json.loads(
                    auth.get('body'))['messages'][0]['message'],
                **self.result)
        if resp.headers['Content-Encoding'] == "gzip":
            r = gzip.decompress(resp.read())
            result = json.loads(r.decode())['value']['data']
        else:
            result = json.loads(resp.read())['value']['data']
        count = 0
        for x in result:
            if int(x['count']) > 0:
                if str(x['epoch2_details']['severity']) == "EVENT_SEVERITY_INFO":
                    continue
                    # with open("output.txt",
                count = count + 1
        if(count != 0):
            self.result['Later Epoch Smart Events'] = result
            self.module.fail_json(
                msg="Pre-change analysis failed. The above smart events have been detected for later epoch only.",
                **self.result)
            return False
        return "Pre-change analysis '%(name)s' passed." % self.params

    def get_delta_result(self):
        if self.get_assurance_group(self.params.get('ag_name')) is None:
            self.module.exit_json(
                msg='No such Assurance Group exists on this fabric.')
        self.params['fabric_id'] = str(
            self.get_assurance_group(
                self.params.get('ag_name'))['uuid'])
        if self.get_delta_analysis() is None:
            self.module.fail_json(
                msg='No such Delta analysis exists.',
                **self.result)
        job_is_done = str(
            self.get_delta_analysis()['status'])
        if job_is_done != "COMPLETED_SUCCESSFULLY":
            self.module.exit_json(
                msg='Delta analysis has not yet completed.', **self.result)
        self.params['uuid'] = str(
            self.get_delta_analysis()['uuid'])
        url = 'https://%(host)s:%(port)s/nae/api/v1/epoch-delta-services/assured-networks/%(fabric_id)s/job/%(uuid)s/health/view/aggregate-table?category=ADC,CHANGE_ANALYSIS,TENANT_ENDPOINT,TENANT_FORWARDING,TENANT_SECURITY,RESOURCE_UTILIZATION,SYSTEM,COMPLIANCE&epoch_status=EPOCH2_ONLY&severity=EVENT_SEVERITY_CRITICAL,EVENT_SEVERITY_MAJOR,EVENT_SEVERITY_MINOR,EVENT_SEVERITY_WARNING,EVENT_SEVERITY_INFO' % self.params
        resp, auth = fetch_url(self.module, url,
                               headers=self.http_headers,
                               data=None,
                               method='GET')
        if auth.get('status') != 200:
            self.module.exit_json(
                msg=json.loads(
                    auth.get('body'))['messages'][0]['message'],
                **self.result)
        if resp.headers['Content-Encoding'] == "gzip":
            r = gzip.decompress(resp.read())
            result = json.loads(r.decode())['value']['data']
        else:
            result = json.loads(resp.read())['value']['data']
        count = 0
        for x in result:
            if int(x['count']) > 0:
                if str(x['epoch2_details']['severity']) == "EVENT_SEVERITY_INFO":
                    continue
                    # with open("output.txt",
                count = count + 1
        if(count != 0):
            self.result['Later Epoch Smart Events'] = result
            self.module.fail_json(
                msg="Delta analysis failed. The above smart events have been detected for later epoch only.",
                **self.result)
            return False
        return "Delta analysis '%(name)s' passed." % self.params

    def create_pre_change_from_manual_changes(self):
        self.params['file'] = None
        self.send_manual_payload()

    def send_manual_payload(self):
        self.params['fabric_id'] = str(
            self.get_assurance_group(
                self.params.get('ag_name'))['uuid'])
        self.params['base_epoch_id'] = str(self.get_epochs()[0]["epoch_id"])
        if '4.1' in self.version:
            f = self.params['file']
            fields = {
                ('data',
                 (f,

                  # content to upload
                  '''{
                                    "name": "''' + self.params.get('name') + '''",
                                    "fabric_uuid": "''' + self.params.get('fabric_id') + '''",
                                    "base_epoch_id": "''' + self.params.get('base_epoch_id') + '''",

                                    "changes": ''' + self.params.get('changes') + ''',
                                    "stop_analysis": false,
                                    "change_type": "CHANGE_LIST"
                                    }'''                            # The content type of the file
                  , 'application/json'))
            }
            url = 'https://%(host)s:%(port)s/nae/api/v1/config-services/prechange-analysis' % self.params
            m = MultipartEncoder(fields=fields)
            h = self.http_headers.copy()
            h['Content-Type'] = m.content_type
            resp, auth = fetch_url(self.module, url,
                                   headers=h,
                                   data=m,
                                   method='POST')

            if auth.get('status') != 200:
                if('filename' in self.params):
                    self.params['file'] = self.params['filename']
                    del self.params['filename']
                self.result['status'] = auth['status']
                self.module.exit_json(msg=json.loads(
                    auth.get('body'))['messages'][0]['message'], **self.result)

            if('filename' in self.params):
                self.params['file'] = self.params['filename']
                del self.params['filename']

            self.result['Result'] = "Pre-change analysis %(name)s successfully created." % self.params

        elif '5.0' in self.version:
            url = 'https://%(host)s:%(port)s/nae/api/v1/config-services/prechange-analysis/manual-changes?action=RUN' % self.params
            form = '''{
                                    "name": "''' + self.params.get('name') + '''",
                                    "allow_unsupported_object_modification": true,
                                    "uploaded_file_name": null,
                                    "stop_analysis": false,
                                    "fabric_uuid": "''' + self.params.get('fabric_id') + '''",
                                    "base_epoch_id": "''' + self.params.get('base_epoch_id') + '''",
                                    "imdata": ''' + self.params.get('changes') + '''
                                    }'''

            resp, auth = fetch_url(self.module, url,
                                   headers=self.http_headers,
                                   data=form,
                                   method='POST')

            if auth.get('status') != 200:
                if('filename' in self.params):
                    self.params['file'] = self.params['filename']
                    del self.params['filename']
                self.result['status'] = auth['status']
                self.module.exit_json(msg=json.loads(
                    auth.get('body'))['messages'][0]['message'], **self.result)

            if('filename' in self.params):
                self.params['file'] = self.params['filename']
                del self.params['filename']

            self.result['Result'] = "Pre-change analysis %(name)s successfully created." % self.params

    def create_pre_change_from_file(self):
        no_parse = False
        if not os.path.exists(self.params.get('file')):
            raise AssertionError("File not found, " +
                                 str(self.params.get('file')))
        filename = self.params.get('file')
        self.params['filename'] = filename
        # self.result['Checking'] = str(self.params.get('filename'))
        # self.module.exit_json(msg="Testing", **self.result)
        f = open(self.params.get('file'), "rb")
        if self.is_json(f.read()) is True:
            no_parse = True
        if self.params['verify'] and no_parse is False:
            # # Input file is not parsed.
            self.params['cmap'] = {}
            data = self.load(open(self.params.get('file')))
            tree = self.construct_tree(data)
            if tree is False:
                self.module.fail_json(
                    msg="Error parsing input file, unsupported object found in heirarchy.",
                    **self.result)
            tree_roots = self.find_tree_roots(tree)
            ansible_ds = {}
            for root in tree_roots:
                exp = self.export_tree(root)
                for key, val in exp.items():
                    ansible_ds[key] = val
            self.copy_children(ansible_ds)
            toplevel = {"totalCount": "1", "imdata": []}
            toplevel['imdata'].append(ansible_ds)
            with open(self.params.get('file'), 'w') as f:
                json.dump(toplevel, f)
            del self.params['cmap']
            f.close()

        # self.result['Checking'] = f
        # self.module.exit_json(msg="Testing", **self.result)
        config = []
        self.params['file'] = f
        self.params['changes'] = config
        self.send_pre_change_payload()

    def copy_children(self, tree):
        '''
        Copies existing children objects to the built tree

        '''
        cmap = self.params['cmap']
        for dn, children in cmap.items():
            aci_class = self.get_aci_class(
                (self.parse_path(dn)[-1]).split("-")[0])
            json_path_expr_search = parse(f'$..children.[*].{aci_class}')
            json_path_expr_update = parse(str([str(match.full_path) for match in json_path_expr_search.find(
                tree) if match.value['attributes']['dn'] == dn][0]))
            curr_obj = [
                match.value for match in json_path_expr_update.find(tree)][0]
            if 'children' in curr_obj:
                for child in children:
                    curr_obj['children'].append(child)
            elif 'children' not in curr_obj:
                curr_obj['children'] = []
                for child in children:
                    curr_obj['children'].append(child)
            json_path_expr_update.update(curr_obj, tree)

        return

    def load(self, fh, chunk_size=1024):
        depth = 0
        in_str = False
        items = []
        buffer = ""

        while True:
            chunk = fh.read(chunk_size)
            if len(chunk) == 0:
                break
            i = 0
            while i < len(chunk):
                c = chunk[i]
                # if i == 0 and c != '[':
                # self.module.fail_json(msg="Input file invalid or already parsed.", **self.result)
                buffer += c

                if c == '"':
                    in_str = not in_str
                elif c == '[':
                    if not in_str:
                        depth += 1
                elif c == ']':
                    if not in_str:
                        depth -= 1
                elif c == '\\':
                    buffer += f[i + 1]
                    i += 1

                if depth == 0:
                    if len(buffer.strip()) > 0:
                        j = json.loads(buffer)
                        assert isinstance(j, list)
                        items += j
                    buffer = ""

                i += 1

        assert depth == 0
        return items

    def parse_path(self, dn):
        """
        Grouping aware extraction of items in a path
        E.g. for /a[b/c/d]/b/c/d/e extracts [a[b/c/d/], b, c, d, e]
        """

        path = []
        buffer = ""
        i = 0
        while i < len(dn):
            if dn[i] == '[':
                while i < len(dn) and dn[i] != ']':
                    buffer += dn[i]
                    i += 1

            if dn[i] == '/':
                path.append(buffer)
                buffer = ""
            else:
                buffer += dn[i]

            i += 1

        path.append(buffer)
        return path

    def construct_tree(self, item_list):
        """
        Given a flat list of items, each with a dn. Construct a tree represeting their relative relationships.
        E.g. Given [/a/b/c/d, /a/b, /a/b/c/e, /a/f, /z], the function will construct

        __root__
          - a (no data)
             - b (data of /a/b)
               - c (no data)
                 - d (data of /a/b/c/d)
                 - e (data of /a/b/c/e)
             - f (data of /a/f)
          - z (data of /z)

        __root__ is a predefined name, you could replace this with a flag root:True/False
        """
        tree = {'data': None, 'name': '__root__', 'children': {}}

        for item in item_list:
            for nm, desc in item.items():
                assert 'attributes' in desc
                attr = desc['attributes']
                assert 'dn' in attr
                if 'children' in desc:
                    existing_children = desc['children']
                    self.params['cmap'][attr['dn']] = existing_children
                path = self.parse_path(attr['dn'])
                cursor = tree
                prev_node = None
                curr_node_dn = ""
                for node in path:
                    curr_node_dn += "/" + str(node)
                    if curr_node_dn[0] == "/":
                        curr_node_dn = curr_node_dn[1:]
                    if node not in cursor['children']:
                        if node == 'uni':
                            cursor['children'][node] = {
                                'data': None,
                                'name': node,
                                'children': {}
                            }
                        else:
                            aci_class_identifier = node.split("-")[0]
                            aci_class = self.get_aci_class(
                                aci_class_identifier)
                            if not aci_class:
                                return False
                            data_dic = {}
                            data_dic['attributes'] = dict(dn=curr_node_dn)
                            cursor['children'][node] = {
                                'data': (aci_class, data_dic),
                                'name': node,
                                'children': {}
                            }
                    cursor = cursor['children'][node]
                    prev_node = node
                cursor['data'] = (nm, desc)
                cursor['name'] = path[-1]

        return tree

    def get_aci_class(self, prefix):
        """
        Contains a hardcoded mapping between dn prefix and aci class.

        E.g for the input identifier prefix of "tn"
        this function will return "fvTenant"

        """

        if prefix == "tn":
            return "fvTenant"
        elif prefix == "epg":
            return "fvAEPg"
        elif prefix == "rscons":
            return "fvRsCons"
        elif prefix == "rsprov":
            return "fvRsProv"
        elif prefix == "rsdomAtt":
            return "fvRsDomAtt"
        elif prefix == "attenp":
            return "infraAttEntityP"
        elif prefix == "rsdomP":
            return "infraRsDomP"
        elif prefix == "ap":
            return "fvAp"
        elif prefix == "BD":
            return "fvBD"
        elif prefix == "subnet":
            return "fvSubnet"
        elif prefix == "rsBDToOut":
            return "fvRsBDToOut"
        elif prefix == "brc":
            return "vzBrCP"
        elif prefix == "subj":
            return "vzSubj"
        elif prefix == "rssubjFiltAtt":
            return "vzRsSubjFiltAtt"
        elif prefix == "flt":
            return "vzFilter"
        elif prefix == "e":
            return "vzEntry"
        elif prefix == "out":
            return "l3extOut"
        elif prefix == "instP":
            return "l3extInstP"
        elif prefix == "extsubnet":
            return "l3extSubnet"
        elif prefix == "rttag":
            return "l3extRouteTagPol"
        elif prefix == "rspathAtt":
            return "fvRsPathAtt"
        elif prefix == "leaves":
            return "infraLeafS"
        elif prefix == "taboo":
            return "vzTaboo"
        elif prefix == "destgrp":
            return "spanDestGrp"
        elif prefix == "srcgrp":
            return "spanSrcGrp"
        elif prefix == "spanlbl":
            return "spanSpanLbl"
        elif prefix == "ctx":
            return "fvCtx"
        else:
            return False

    def find_tree_roots(self, tree):
        """
        Find roots for tree export. This involves finding all "fake" (dataless) nodes.

        E.g. for the tree
        __root__
          - a (no data)
             - b (data of /a/b)
               - c (no data)
                 - d (data of /a/b/c/d)
                 - e (data of /a/b/c/e)
             - f (data of /a/f)
          - z (data of /z)

        This function will return [__root__, a, c]
        """
        if tree['data'] is not None:
            return [tree]

        roots = []
        for child in tree['children'].values():
            roots += self.find_tree_roots(child)

        return roots

    def export_tree(self, tree):
        """
        Exports the constructed tree to a heirachial json representation. (equal to tn-ansible, except for ordering)
        """
        tree_data = {
            'attributes': tree['data'][1]['attributes']
        }
        children = []
        for child in tree['children'].values():
            children.append(self.export_tree(child))

        if len(children) > 0:
            tree_data['children'] = children

        return {tree['data'][0]: tree_data}

    def delete_pre_change_analysis(self):
        if self.get_pre_change_analysis() is None:
            self.module.exit_json(msg='No such Pre-Change Job exists.')
        self.params['job_id'] = str(self.get_pre_change_analysis()['job_id'])

        url = 'https://%(host)s:%(port)s/nae/api/v1/config-services/prechange-analysis/%(job_id)s' % self.params
        resp, auth = fetch_url(self.module, url,
                               headers=self.http_headers,
                               data=None,
                               method='DELETE')

        if auth.get('status') != 200:
            if('filename' in self.params):
                self.params['file'] = self.params['filename']
                del self.params['filename']
            self.module.exit_json(
                msg=json.loads(
                    auth.get('body'))['messages'][0]['message'],
                **self.result)

        self.result['msg'] = json.loads(resp.read())['value']['data']

    def get_epochs(self):
        self.params['fabric_id'] = str(
            self.get_assurance_group(
                self.params.get('ag_name'))['uuid'])
        url = 'https://%(host)s:%(port)s/nae/api/v1/event-services/assured-networks/%(fabric_id)s/epochs?$sort=-collectionTimestamp' % self.params
        resp, auth = fetch_url(self.module, url,
                               headers=self.http_headers,
                               data=None,
                               method='GET')

        if auth.get('status') != 200:
            if('filename' in self.params):
                self.params['file'] = self.params['filename']
                del self.params['filename']
            self.module.exit_json(
                msg=json.loads(
                    auth.get('body'))['messages'][0]['message'],
                **self.result)

        if resp.headers['Content-Encoding'] == "gzip":
            r = gzip.decompress(resp.read())
            return json.loads(r.decode())['value']['data']
        return json.loads(resp.read())['value']['data']

    def send_pre_change_payload(self):
        self.params['fabric_id'] = str(
            self.get_assurance_group(
                self.params.get('ag_name'))['uuid'])
        self.params['base_epoch_id'] = str(self.get_epochs()[0]["epoch_id"])
        f = self.params.get('file')
        payload = {
            "name": self.params.get('name'),
            "fabric_uuid": self.params.get('fabric_id'),
            "base_epoch_id": self.params.get('base_epoch_id'),
            "stop_analysis": False
        }

        if '4.1' in self.version:
            payload['change_type'] = "CONFIG_FILE"
            payload['changes'] = self.params.get('changes')
            url = 'https://%(host)s:%(port)s/nae/api/v1/config-services/prechange-analysis' % self.params

        elif '5.0' in self.version:
            payload['allow_unsupported_object_modification'] = 'true'
            payload['uploaded_file_name'] = str(self.params.get('filename'))
            url = 'https://%(host)s:%(port)s/nae/api/v1/config-services/prechange-analysis/file-changes' % self.params

        files = {"file": (str(self.params.get('filename')),
                          open(str(self.params.get('filename')),
                               'rb'),
                          'application/json'),
                 "data": ("blob",
                          json.dumps(payload),
                          'application/json')}

        m = MultipartEncoder(fields=files)

        # Need to set the right content type for the multi part upload!
        h = self.http_headers.copy()
        h['Content-Type'] = m.content_type

        resp, auth = fetch_url(self.module, url,
                               headers=h,
                               data=m,
                               method='POST')

        if auth.get('status') != 200:
            if('filename' in self.params):
                self.params['file'] = self.params['filename']
                del self.params['filename']
            self.result['status'] = auth['status']
            self.module.exit_json(msg=json.loads(
                auth.get('body'))['messages'][0]['message'], **self.result)

        if('filename' in self.params):
            self.params['file'] = self.params['filename']
            del self.params['filename']

        self.result['Result'] = "Pre-change analysis %(name)s successfully created." % self.params

    def new_object_selector(self):
        self.params['fabric_uuid'] = self.getFirstAG()["uuid"]
        url = 'https://%(host)s:%(port)s/nae/api/v1/event-services/assured-networks/%(fabric_uuid)s/model/aci-policy/compliance-requirement/object-selectors' % self.params
        resp, auth = fetch_url(self.module, url,
                               data=self.params['form'],
                               headers=self.http_headers,
                               method='POST')
        if auth.get('status') != 200:
            if('filename' in self.params):
                self.params['file'] = self.params['filename']
                del self.params['filename']
            self.status = auth.get('status')
            try:
                self.module.fail_json(msg=json.loads(auth.get('body'))[
                                      'messages'][0]['message'], **self.result)
            except KeyError:
                # Connection error
                self.fail_json(
                    msg='Connection failed for %(url)s. %(msg)s' %
                    auth, **self.result)
        else:
            final_msg = "Object Selector " + \
                str(json.loads(resp.read())['value']
                    ['data']['name']) + " created"
            self.result['Result'] = final_msg

    def new_traffic_selector(self):
        self.params['fabric_uuid'] = self.getFirstAG()["uuid"]
        url = 'https://%(host)s:%(port)s/nae/api/v1/event-services/assured-networks/%(fabric_uuid)s/model/aci-policy/compliance-requirement/traffic-selectors' % self.params
        resp, auth = fetch_url(self.module, url,
                               data=self.params['form'],
                               headers=self.http_headers,
                               method='POST')
        if auth.get('status') != 200:
            if('filename' in self.params):
                self.params['file'] = self.params['filename']
                del self.params['filename']
            self.status = auth.get('status')
            try:
                self.module.fail_json(msg=json.loads(auth.get('body'))[
                                      'messages'][0]['message'], **self.result)
            except KeyError:
                # Connection error
                self.fail_json(
                    msg='Connection failed for %(url)s. %(msg)s' %
                    auth, **self.result)
        else:
            final_msg = "Traffic Selector " + \
                str(json.loads(resp.read())['value']
                    ['data']['name']) + " created"
            self.result['Result'] = final_msg

    def new_compliance_requirement(self):
        self.params['fabric_uuid'] = self.getFirstAG()["uuid"]
        url = 'https://%(host)s:%(port)s/nae/api/v1/event-services/assured-networks/%(fabric_uuid)s/model/aci-policy/compliance-requirement/requirements' % self.params
        resp, auth = fetch_url(self.module, url,
                               data=self.params['form'],
                               headers=self.http_headers,
                               method='POST')
        if auth.get('status') != 200:
            if('filename' in self.params):
                self.params['file'] = self.params['filename']
                del self.params['filename']
            self.status = auth.get('status')
            try:
                self.module.fail_json(msg=json.loads(auth.get('body'))[
                                      'messages'][0]['message'], **self.result)
            except KeyError:
                # Connection error
                self.fail_json(
                    msg='Connection failed for %(url)s. %(msg)s' %
                    auth, **self.result)
        else:
            final_msg = "Compliance requirement " + \
                str(json.loads(resp.read())['value']
                    ['data']['name']) + " created"
            self.result['Result'] = final_msg

    def new_compliance_requirement_set(self):
        ag = self.get_assurance_group(self.params.get('ag_name'))["uuid"]
        d = json.loads(self.params['form'])
        assurance_groups_lists = []
        assurance_groups_lists.append(dict(active=True, fabric_uuid=ag))
        d['assurance_groups'] = assurance_groups_lists
        self.params['form'] = json.dumps(d)
        self.params['fabric_uuid'] = self.getFirstAG()["uuid"]
        url = 'https://%(host)s:%(port)s/nae/api/v1/event-services/assured-networks/%(fabric_uuid)s/model/aci-policy/compliance-requirement/requirement-sets' % self.params
        resp, auth = fetch_url(self.module, url,
                               data=self.params['form'],
                               headers=self.http_headers,
                               method='POST')
        if auth.get('status') != 200:
            if('filename' in self.params):
                self.params['file'] = self.params['filename']
                del self.params['filename']
            self.status = auth.get('status')
            try:
                self.module.fail_json(msg=auth.get('body'), **self.result)
            except KeyError:
                # Connection error
                self.fail_json(
                    msg='Connection failed for %(url)s. %(msg)s' %
                    auth, **self.result)
        else:
            final_msg = "Compliance requirement set " + \
                str(json.loads(resp.read())['value']
                    ['data']['name']) + " created"
            self.result['Result'] = final_msg

    def get_all_requirement_sets(self):
        self.params['fabric_uuid'] = self.getFirstAG()["uuid"]
        url = 'https://%(host)s:%(port)s/nae/api/v1/event-services/assured-networks/%(fabric_uuid)s/model/aci-policy/compliance-requirement/requirement-sets' % self.params
        resp, auth = fetch_url(self.module, url,
                               headers=self.http_headers,
                               method='GET')
        if auth.get('status') != 200:
            if('filename' in self.params):
                self.params['file'] = self.params['filename']
                del self.params['filename']
            self.status = auth.get('status')
            try:
                self.module.fail_json(msg=auth.get('body'), **self.result)
            except KeyError:
                # Connection error
                self.fail_json(
                    msg='Connection failed for %(url)s. %(msg)s' %
                    auth, **self.result)
        else:
            if resp.headers['Content-Encoding'] == "gzip":
                r = gzip.decompress(resp.read())
                self.result['Result'] = json.loads(r.decode())['value']['data']
                return json.loads(r.decode())['value']['data']
            self.result['Result'] = json.loads(resp.read())['value']['data']
            return json.loads(resp.read())['value']['data']

    def get_all_requirements(self):
        self.params['fabric_uuid'] = self.getFirstAG()["uuid"]
        url = 'https://%(host)s:%(port)s/nae/api/v1/event-services/assured-networks/%(fabric_uuid)s/model/aci-policy/compliance-requirement/requirements' % self.params
        resp, auth = fetch_url(self.module, url,
                               headers=self.http_headers,
                               method='GET')
        if auth.get('status') != 200:
            if('filename' in self.params):
                self.params['file'] = self.params['filename']
                del self.params['filename']
            self.status = auth.get('status')
            try:
                self.module.fail_json(msg=auth.get('body'), **self.result)
            except KeyError:
                # Connection error
                self.fail_json(
                    msg='Connection failed for %(url)s. %(msg)s' %
                    auth, **self.result)
        else:
            if resp.headers['Content-Encoding'] == "gzip":
                r = gzip.decompress(resp.read())
                self.result['Result'] = json.loads(r.decode())['value']['data']
                return json.loads(r.decode())['value']['data']
            self.result['Result'] = json.loads(resp.read())['value']['data']
            return json.loads(resp.read())['value']['data']

    def get_all_traffic_selectors(self):
        self.params['fabric_uuid'] = self.getFirstAG()["uuid"]
        url = 'https://%(host)s:%(port)s/nae/api/v1/event-services/assured-networks/%(fabric_uuid)s/model/aci-policy/compliance-requirement/traffic-selectors' % self.params
        resp, auth = fetch_url(self.module, url,
                               headers=self.http_headers,
                               method='GET')
        if auth.get('status') != 200:
            if('filename' in self.params):
                self.params['file'] = self.params['filename']
                del self.params['filename']
            self.status = auth.get('status')
            try:
                self.module.fail_json(msg=auth.get('body'), **self.result)
            except KeyError:
                # Connection error
                self.fail_json(
                    msg='Connection failed for %(url)s. %(msg)s' %
                    auth, **self.result)
        else:
            if resp.headers['Content-Encoding'] == "gzip":
                r = gzip.decompress(resp.read())
                self.result['Result'] = json.loads(r.decode())['value']['data']
                return json.loads(r.decode())['value']['data']
            self.result['Result'] = json.loads(resp.read())['value']['data']
            return json.loads(resp.read())['value']['data']

    def get_all_object_selectors(self):
        self.params['fabric_uuid'] = self.getFirstAG()["uuid"]
        url = 'https://%(host)s:%(port)s/nae/api/v1/event-services/assured-networks/%(fabric_uuid)s/model/aci-policy/compliance-requirement/object-selectors' % self.params
        resp, auth = fetch_url(self.module, url,
                               headers=self.http_headers,
                               method='GET')
        if auth.get('status') != 200:
            if('filename' in self.params):
                self.params['file'] = self.params['filename']
                del self.params['filename']
            self.status = auth.get('status')
            try:
                self.module.fail_json(msg=auth.get('body'), **self.result)
            except KeyError:
                # Connection error
                self.fail_json(
                    msg='Connection failed for %(url)s. %(msg)s' %
                    auth, **self.result)
        else:
            if resp.headers['Content-Encoding'] == "gzip":
                r = gzip.decompress(resp.read())
                self.result['Result'] = json.loads(r.decode())['value']['data']
                return json.loads(r.decode())['value']['data']
            self.result['Result'] = json.loads(resp.read())['value']['data']
            return json.loads(r.decode())['value']['data']

    def get_compliance_object(self, name):
        if self.params.get('selector') == 'object':
            objs = self.get_all_object_selectors()
            return [x for x in objs if x['name'] == name][0]
        elif self.params.get('selector') == 'traffic':
            objs = self.get_all_traffic_selectors()
            return [x for x in objs if x['name'] == name][0]
        elif self.params.get('selector') == 'requirement':
            objs = self.get_all_requirements()
            return [x for x in objs if x['name'] == name][0]
        elif self.params.get('selector') == 'requirement_sets':
            objs = self.get_all_requirement_sets()
            return [x for x in objs if x['name'] == name][0]

    def delete_object_selector(self):
        self.params['fabric_uuid'] = self.getFirstAG()["uuid"]
        self.params['obj_uuid'] = self.get_compliance_object(
            self.params.get('name'))["uuid"]
        url = 'https://%(host)s:%(port)s/nae/api/v1/event-services/assured-networks/%(fabric_uuid)s/model/aci-policy/compliance-requirement/object-selectors/%(obj_uuid)s' % self.params
        resp, auth = fetch_url(self.module, url,
                               headers=self.http_headers,
                               method='DELETE')
        if auth.get('status') != 200:
            if('filename' in self.params):
                self.params['file'] = self.params['filename']
                del self.params['filename']
            self.status = auth.get('status')
            try:
                self.module.fail_json(msg=auth.get('body'), **self.result)
            except KeyError:
                # Connection error
                self.fail_json(
                    msg='Connection failed for %(url)s. %(msg)s' %
                    auth, **self.result)
        else:
            self.result['Result'] = "Object selector " + \
                self.params.get('name') + " deleted"

    def delete_traffic_selector(self):
        self.params['fabric_uuid'] = self.getFirstAG()["uuid"]
        self.params['obj_uuid'] = self.get_compliance_object(
            self.params.get('name'))["uuid"]
        url = 'https://%(host)s:%(port)s/nae/api/v1/event-services/assured-networks/%(fabric_uuid)s/model/aci-policy/compliance-requirement/traffic-selectors/%(obj_uuid)s' % self.params
        resp, auth = fetch_url(self.module, url,
                               headers=self.http_headers,
                               method='DELETE')
        if auth.get('status') != 200:
            if('filename' in self.params):
                self.params['file'] = self.params['filename']
                del self.params['filename']
            self.status = auth.get('status')
            try:
                self.module.fail_json(msg=auth.get('body'), **self.result)
            except KeyError:
                # Connection error
                self.fail_json(
                    msg='Connection failed for %(url)s. %(msg)s' %
                    auth, **self.result)
        else:
            self.result['Result'] = "Traffic selector " + \
                self.params.get('name') + " deleted"

    def delete_requirement(self):
        self.params['fabric_uuid'] = self.getFirstAG()["uuid"]
        self.params['obj_uuid'] = self.get_compliance_object(
            self.params.get('name'))["uuid"]
        url = 'https://%(host)s:%(port)s/nae/api/v1/event-services/assured-networks/%(fabric_uuid)s/model/aci-policy/compliance-requirement/requirements/%(obj_uuid)s' % self.params
        resp, auth = fetch_url(self.module, url,
                               headers=self.http_headers,
                               method='DELETE')
        if auth.get('status') != 200:
            if('filename' in self.params):
                self.params['file'] = self.params['filename']
                del self.params['filename']
            self.status = auth.get('status')
            try:
                self.module.fail_json(msg=auth.get('body'), **self.result)
            except KeyError:
                # Connection error
                self.fail_json(
                    msg='Connection failed for %(url)s. %(msg)s' %
                    auth, **self.result)
        else:
            self.result['Result'] = "Requirement " + \
                self.params.get('name') + " deleted"

    def delete_requirement_set(self):
        self.params['fabric_uuid'] = self.getFirstAG()["uuid"]
        self.params['obj_uuid'] = self.get_compliance_object(
            self.params.get('name'))["uuid"]
        url = 'https://%(host)s:%(port)s/nae/api/v1/event-services/assured-networks/%(fabric_uuid)s/model/aci-policy/compliance-requirement/requirement_set/%(obj_uuid)s' % self.params
        resp, auth = fetch_url(self.module, url,
                               headers=self.http_headers,
                               method='DELETE')
        if auth.get('status') != 200:
            if('filename' in self.params):
                self.params['file'] = self.params['filename']
                del self.params['filename']
            self.status = auth.get('status')
            try:
                self.module.fail_json(msg=auth.get('body'), **self.result)
            except KeyError:
                # Connection error
                self.fail_json(
                    msg='Connection failed for %(url)s. %(msg)s' %
                    auth, **self.result)
        else:
            self.result['Result'] = "Requirement set " + \
                self.params.get('name') + " deleted"

    def getFirstAG(self):
        self.get_all_assurance_groups()
        return self.assuranceGroups[0]

    def upload_file(self):
        self.params['fabric_uuid'] = self.getFirstAG()["uuid"]
        file_upload_uuid = None
        uri = 'https://%(host)s:%(port)s/nae/api/v1/file-services/upload-file' % self.params
        try:
            with self.get_logout_lock():
                chunk_url = self.start_upload(uri, 'OFFLINE_ANALYSIS')
                complete_url = None
                if chunk_url:
                    complete_url = self.upload_file_by_chunk(chunk_url)
                else:
                    self.fail_json(msg='Error', **self.result)
                if complete_url:
                    file_upload_uuid = self.complete_upload(complete_url)[
                        'uuid']
                else:
                    self.fail_json(
                        msg='Failed to upload file chunks', **self.result)
            return file_upload_uuid
        except Exception as e:
            self.fail_json(msg='Failed to upload file chunks', **self.result)

        return all_files_status

    def start_upload(self, uri, upload_type):
        """
        Pass metadata to api and trigger start of upload file.
        Args:
            unique_name: str: name of upload
            file_name:  str:  file name of upload
            file_path:  str: path of file
            fabric_uuid: str: offline fabric id
            uri: str: uri
            upload_type: str: offline file/nat file
        Returns:
            str: chunk url , used for uploading chunks
                  or None if there was an issue starting
        """
        file_size_in_bytes = os.path.getsize(self.params.get('file'))
        if not file_name:
            file_name = os.path.basename(self.params.get('file'))
        args = {"data": {"unique_name": self.params.get('name'),
                         "filename": file_name,
                         "size_in_bytes": int(file_size_in_bytes),
                         "upload_type": upload_type}}  # "OFFLINE_ANALYSIS"

        resp, auth = fetch_url(self.module, uri,
                               data=json.dumps(args['data']),
                               headers=self.http_headers,
                               method='POST')
        if auth.get('status') != 200:
            self.status = auth.get('status')
            try:
                self.module.fail_json(msg=auth.get('body'), **self.result)
            except KeyError:
                # Connection error
                self.fail_json(
                    msg='Connection failed for %(url)s. %(msg)s' %
                    auth, **self.result)
        elif auth.get('status') == 201:
            return str(json.loads(resp.read())['value']['data']['links'][-1]['href'])
        return None

    def upload_file_by_chunk(self, chunk_url):
        """Pass metadata to api and trigger start of upload file.
        Args:
           chunk_url: str: url to send chunks
           file_path: str: path of file and filename
        Returns:
            str: chunk url , used for uploading chunks or None if issue uploading
        """
        try:
            chunk_id = 0
            offset = 0
            chunk_uri = 'https://%(host)s:%(port)s/nae' % self.params
            chunk_uri = chunk_uri + chunk_url[chunk_url.index('/api/'):]
            response = None
            file_size_in_bytes = os.path.getsize(self.params.get('file'))
            chunk_byte_size = 10000000
            if file_size_in_bytes < chunk_byte_size:
                chunk_byte_size = int(file_size_in_bytes // 2)
            with open(self.params.get('file'), 'rb') as f:
                for chunk in self.read_in_chunks(f, chunk_byte_size):
                    checksum = hashlib.md5(chunk).hexdigest()
                    chunk_info = {"offset": int(offset),
                                  "checksum": checksum,
                                  "chunk_id": chunk_id,
                                  "size_in_bytes": sys.getsizeof(chunk)}
                    files = {"chunk-info": (None, json.dumps(chunk_info),
                                            'application/json'),
                             "chunk-data": (os.path.basename(self.params.get('file')) +
                                            str(chunk_id),
                                            chunk, 'application/octet-stream')}
                    args = {"files": files}
                    chunk_headers = self.http_headers.copy()
                    chunk_headers.pop("Content-Type", None)
                    resp, auth = fetch_url(self.module, uri,
                                           data=None,
                                           headers=chunk_headers,
                                           files=args['files'],
                                           method='POST')
                    chunk_id += 1
                    if resp and auth.get('status') != 201:
                        self.module.fail_json(
                            msg="Incorrect response code", **self.result)
                        return None
                if response:
                    return str(json.loads(resp.read())['value']['data']['links'][-1]['href'])
                else:
                    self.module.fail_json(
                        msg="No reponse received while uploading chunks", **self.result)
        except IOError as ioex:
            self.module.fail_json(
                msg="Cannot open supplied file", **self.result)
        return None

    def read_in_chunks(self, file_object, chunk_byte_size):
        """
        Return chunks of file.
        Args:
           file_object: file: open file object
           chunk_byte_size: int: size of chunk to return
        Returns:
            Returns a chunk of the file
        """
        while True:
            data = file_object.read(chunk_byte_size)
            if not data:
                break
            yield data

    def complete_upload(self, complete_url):
        """Complete request to start dag.

        Args:
           chunk_url: str: url to complte upload and start dag

        Returns:
            str: uuid or None

        NOTE: Modified function to not fail if epoch is at scale.
        Scale epochs sometimes take longer to upload and in that
        case, the api returns a timeout even though the upload
        completes successfully later.
        """
        timeout = 300
        complete_uri = 'https://%(host)s:%(port)s/nae' % self.params
        complete_uri = complete_uri + \
            complete_url[complete_url.index('/api/'):]
        resp, auth = fetch_url(self.module, complete_uri,
                               data=None,
                               headers=self.http_headers,
                               method='POST')
        try:
            if resp and auth.get('status') == 200:
                return str(json.loads(resp.read())['value']['data']['links'][-1]['href'])
            elif not resp or auth.get('status') == 400:
                total_time = 0
                while total_time < timeout:
                    time.sleep(10)
                    total_time += 10
                    resp, auth = fetch_url(
                        self.module, 'https://%(host)s:%(port)s/nae/api/v1/file-services/upload-file', data=None, method='GET')
                    if resp and auth.get('status') == 200:
                        json.loads(resp.read())
                        uuid = complete_url.split('/')[-2]
                        for offline_file in resp['value']['data']:
                            if offline_file['uuid'] == uuid:
                                success = offline_file['status'] == 'UPLOAD_COMPLETED'
                                if success:
                                    return {'uuid': offline_file['uuid']}

            self.module.fail_json(msg="No upload complete", **self.result)
            raise Exception
        except Exception as e:
            self.module.fail_json(msg="Unknown error", **self.result)

    def isLiveAnalysis(self):
        self.get_all_assurance_groups()
        for ag in self.assuranceGroups:
            if ag['status'] == "RUNNING" and 'iterations' not in ag:
                return ag['unique_name']

    def isOnDemandAnalysis(self):
        self.get_all_assurance_groups()
        for ag in self.assuranceGroups:
            if (ag['status'] == "RUNNING" or ag['status'] == "ANALYSIS_NOT_STARTED" or ag['status'] == "ANALYSIS_IN_PROGRESS") and ('iterations' in ag):
                return ag['unique_name']

    def get_tcam_stats(self):
        self.params['fabric_id'] = str(
            self.get_assurance_group(
                self.params.get('ag_name'))['uuid'])
        self.params['latest_epoch'] = str(self.get_epochs()[-1]["epoch_id"])
        self.params['page'] = 0
        self.params['obj_per_page'] = 200
        has_more_data = True
        tcam_data = []
        # As long as there is more data get it
        while has_more_data:
            # I get data sorter by tcam hists for hitcount-by-rules --> hitcount-by-epgpair-contract-filter
            url = 'https://%(host)s:%(port)s/nae/api/v1/event-services/assured-networks/%(fabric_id)s/model/aci-policy/tcam/hitcount-by-rules/hitcount-by-epgpair-contract-filter?$epoch_id=%(latest_epoch)s&$page=%(page)s&$sort=-cumulative_count&$view=histogram' % self.params
            resp, auth = fetch_url(
                self.module, url, headers=self.http_headers, method='GET')
            if auth.get('status') != 200:
                self.result['Error'] = auth.get('msg')
                self.result['url'] = url
                self.module.fail_json(msg="Error getting TCAM", **self.result)
            if resp.headers['Content-Encoding'] == "gzip":
                r = gzip.decompress(resp.read())
                has_more_data = json.loads(r.decode())[
                    'value']['data_summary']['has_more_data']
                tcam_data.append(json.loads(r.decode())['value']['data'])
            else:
                has_more_data = json.loads(resp.read())[
                    'value']['data_summary']['has_more_data']
                tcam_data.append(json.loads(resp.read())['value']['data'])
            self.params['page'] = self.params['page'] + 1

        self.result['Result'] = 'Pages extracted %(page)s ' % self.params
        return tcam_data

    def tcam_to_csv(self):
        tcam_data = self.get_tcam_stats()
        tcam_stats = []
        for page in tcam_data:
            for item in page:
                tdic = {}
                for key, value in item.items():
                    if key == "bucket":
                        tdic['Provider EPG'] = value['provider_epg']['dn'].replace(
                            "uni/", "")
                        tdic['Consumer VRF'] = value['consumer_vrf']['dn'].replace(
                            "uni/", "")
                        tdic['Consumer EPG'] = value['consumer_epg']['dn'].replace(
                            "uni/", "")
                        tdic['Contract'] = value['contract']['dn'].replace(
                            "uni/", "")
                        tdic['Filter'] = value['filter']['dn'].replace(
                            "uni/", "")
                    if key == "output":
                        if 'month_count' in value:
                            tdic["Monthly Hits"] = value['month_count']
                        else:
                            tdic["Monthly Hits"] = "N/A"
                        tdic['Total Hits'] = value['cumulative_count']
                        tdic['TCAM Usage'] = value['tcam_entry_count']
                tcam_stats.append(tdic)
        outfile = self.params.get('file') + '.csv'
        with open(outfile, 'w', newline='') as f:
            fieldnames = ['Provider EPG', 'Consumer EPG', 'Consumer VRF',
                          'Contract', 'Filter', 'Monthly Hits', 'Total Hits', 'TCAM Usage']
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            writer.writeheader()
            for i in tcam_stats:
                writer.writerow(i)
        success = 'to file %(file)s.csv' % self.params
        self.result['Result'] = self.result['Result'] + success

    def StartOnDemandAnalysis(self, iterations):
        runningLive = self.isLiveAnalysis()
        runningOnDemand = self.isOnDemandAnalysis()
        if runningLive:
            self.module.fail_json(
                msg=f'There is currently a Live analysis on {runningLive} please stop it manually and try again', **self.result)

        elif runningOnDemand:
            self.module.fail_json(
                msg=f'There is currently an OnDemand analysis running on {runningOnDemand} please stop it manually and try again', **self.result)
        else:
            self.fabric_uuid = self.get_assurance_group(
                self.params.get('ag_name'))

            if ag == None:
                self.module.fail_json(
                    msg="Assurance group does not exist", **self.result)

            ag_iterations = json.dumps({'iterations': iterations})
            url = 'https://%(host)s:%(port)s/nae/api/v1/config-services/assured-networks/aci-fabric/%(fabric_uuid)s/start-analysis' % self.params
            resp, auth = fetch_url(self.module, url,
                                   data=ag_iterations,
                                   headers=self.http_headers,
                                   method='POST')
            if auth.get('status') == 200:
                self.result[
                    'Result'] = 'Successfully started OnDemand Analysis on %(ag_name)s' % self.params

            else:
                self.module.fail_json(
                    msg="OnDemand Analysis failed to start", **self.result)

    def get_delta_analysis(self):
        ret = self.get_delta_analyses()
        for a in ret:
            if a['unique_name'] == self.params.get('name'):
                # self.result['analysis'] = a
                return a
        return None

    def query_delta_analyses(self):
        self.result['Delta analyses'] = self.get_delta_analyses()

    def get_delta_analyses(self):
        self.params['fabric_id'] = str(
            self.get_assurance_group(
                self.params.get('ag_name'))['uuid'])
        url = 'https://%(host)s/nae/api/v1/job-services?$page=0&$size=100&$sort=status&$type=EPOCH_DELTA_ANALYSIS&assurance_group_id=%(fabric_id)s' % self.params
        resp, auth = fetch_url(self.module, url, data=None,
                               headers=self.http_headers, method='GET')
        return json.loads(resp.read())['value']['data']

    def delete_delta_analysis(self):
        self.params['fabric_id'] = str(
            self.get_assurance_group(
                self.params.get('ag_name'))['uuid'])
        try:
            self.params['analysis_id'] = [analysis for analysis in self.get_delta_analyses(
            ) if analysis['unique_name'] == self.params.get('name')][0]['uuid']
        except IndexError:
            fail = "Delta analysis %(name)s does not exist on %(ag_name)s." % self.params
            self.module.fail_json(msg=fail, **self.result)

        url = 'https://%(host)s/nae/api/v1/job-services/%(analysis_id)s' % self.params
        resp, auth = fetch_url(self.module, url, data=None,
                               headers=self.http_headers, method='DELETE')
        if 'OK' in auth.get('msg'):
            self.result['Result'] = 'Delta analysis %(name)s successfully deleted' % self.params
        else:
            fail = "Delta analysis deleted failed " + auth.get('msg')
            self.module.fail_json(msg=fail, **self.result)

    def new_delta_analysis(self):
        fabric_id = str(
            self.get_assurance_group(
                self.params.get('ag_name'))['uuid'])
        epochs = list(self.get_epochs())
        e = [epoch for epoch in epochs if epoch['fabric_id'] == fabric_id]
        later_epoch_uuid = e[0]['epoch_id']
        prior_epoch_uuid = e[1]['epoch_id']
        url = 'https://%(host)s/nae/api/v1/job-services' % self.params
        form = '''{
               "type": "EPOCH_DELTA_ANALYSIS",
               "name": "''' + self.params.get('name') + '''",
               "parameters": [
                   {
                       "name": "prior_epoch_uuid",
                       "value": "''' + str(prior_epoch_uuid) + '''"
                   },
                   {
                       "name": "later_epoch_uuid",
                       "value": "''' + str(later_epoch_uuid) + '''"
                   }
                   ]
               }'''
        resp, auth = fetch_url(self.module, url, data=form,
                               headers=self.http_headers, method='POST')

        if 'OK' in auth.get('msg'):
            self.result['Result'] = 'Delta analysis %(name)s successfully created' % self.params
        else:
            fail = "Delta analysis creation failed " + auth.get('msg')
            self.module.fail_json(msg=fail, **self.result)

    # def newOfflineAnalysis(self, name, fileID, fabricID):
        # self.logger.info("Trying to Starting Analysis  %s",name)

        # while self.isOnDemandAnalysis() or self.isLiveAnalysis():
            # self.module.fail_json(msg="There is currently an  analysis running.",**self.result)

        # form = '''{
          # "unique_name": "''' + name + '''",
          # "file_upload_uuid": "''' + fileID +'''",
          # "aci_fabric_uuid": "''' + fabricID + '''",
          # "analysis_timeout_in_secs": 3600
        # }'''

        # if '4.0' in self.version:
            # url ='https://'+self.ip_addr+'/nae/api/v1/event-services/offline-analysis'
            # req = requests.post(url, data=form,  headers=self.http_headers, cookies=self.session_cookie, verify=False)
            # if req.status_code == 202:
            # self.logger.info("Offline Analysis %s Started", name)
            # else:
            # self.logger.info("Offline Analysis creation failed with error message \n %s",req.content)

        # elif '4.1' in self.version or '5.0' in  self.version or '5.1' in self.version:
            # #in 4.1 starting an offline analysis is composed of 2 steps
            # # 1 Create the Offline analysis
            # url ='https://'+self.ip_addr+'/nae/api/v1/config-services/offline-analysis'
            # req = requests.post(url, data=form,  headers=self.http_headers, cookies=self.session_cookie, verify=False)
            # if req.status_code == 202:
            # self.logger.info("Offline Analysis %s Created", name)
            # pprint(req.json()['value']['data'])
            # #Get the analysis UUID:
            # analysis_id = req.json()['value']['data']['uuid']

            # url ='https://'+self.ip_addr+'/nae/api/v1/config-services/analysis'

            # form = '''{
            # "interval": 300,
            # "type": "OFFLINE",
            # "assurance_group_list": [
            # {
            # "uuid": "''' + fabricID + '''"
            # }
            # ],
            # "offline_analysis_list": [
            # {
            # "uuid":"''' + analysis_id + '''"
            # }
            # ],
            # "iterations": 1
            # }'''

            # req = requests.post(url, data=form,  headers=self.http_headers, cookies=self.session_cookie, verify=False)
            # if req.status_code == 202 or req.status_code == 200 :
            # self.logger.info("Offline Analysis %s Started", name)
            # #Sleeping 10s as it takes a moment for the status to be updated.
            # time.sleep(10)
            # else:
            # self.logger.info("Offline Analysis creation failed with error message \n %s",req.content)

        # else:
            # self.logger.info("Unsupported version")

    # def getFiles(self):
        # #This methods loads all the uploaded files to NAE
        # url = 'https://'+self.ip_addr+'/nae/api/v1/file-services/upload-file'
        # req = requests.get(url, headers=self.http_headers, cookies=self.session_cookie, verify=False)
        # self.files = req.json()['value']['data']