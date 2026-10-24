import copy
import unittest
from workshop.resource_state import compare, equivalent


class ResourceStateTests(unittest.TestCase):
    def setUp(self):
        self.before={'name':'projects/participant-project/locations/us/dataAgents/agent-one','createTime':'2026-09-01T00:00:00Z',
                     'updateTime':'old','etag':'old','dataAnalyticsAgent':{'publishedContext':{'description':'rule'}}}

    def test_only_allowed_top_level_metadata_is_ignored(self):
        after=copy.deepcopy(self.before);after.update(updateTime='new',etag='new')
        result=compare(self.before,after);self.assertTrue(result['equivalent'])
        self.assertEqual(set(result['metadata_changes']),{'updateTime','etag'})
        after['dataAnalyticsAgent']['updateTime']='new'
        self.assertFalse(equivalent(self.before,after))

    def test_recreated_resource_unknown_fields_and_identity_are_not_ignored(self):
        for key,value in (('name',self.before['name']+'-new'),('createTime','new'),('uid','new'),('unknown',True),('labels',{})):
            with self.subTest(key=key):
                after={**self.before,key:value};self.assertFalse(equivalent(self.before,after))

    def test_missing_identity_cannot_allow_metadata_drift(self):
        self.before.pop('createTime');after={**self.before,'updateTime':'new'}
        self.assertFalse(equivalent(self.before,after))
        self.assertTrue(equivalent(self.before,self.before))

    def test_absent_resource_and_unknown_resource_types_are_strict(self):
        self.assertFalse(equivalent(None,None));self.assertFalse(equivalent({},{}))
        self.before['name']='projects/participant-project/datasets/ec_workshop/tables/orders'
        self.assertFalse(equivalent(self.before,{**self.before,'updateTime':'new'}))

    def test_metadata_deletion_is_recorded_with_presence(self):
        after=copy.deepcopy(self.before);after.pop('etag')
        delta=compare(self.before,after)['metadata_changes']['etag']
        self.assertTrue(delta['before_present']);self.assertFalse(delta['after_present'])

    def test_listed_api_defaults_match_absent_fields_only(self):
        group={'name':'projects/participant-project/locations/us/entryGroups/ec-lab1-x','createTime':'c','displayName':'EC'}
        setting={'name':'projects/participant-project/locations/us/gibqObservabilitySettings/ec-workshop-x','createTime':'c',
                 'conversationalAnalyticsSetting':{'metricsEnabled':True,'tracesEnabled':True}}
        for before,path,default in ((group,('changeRequestConfig',),{}),(setting,('conversationalAnalyticsSetting','bqaaEnabled'),False)):
            with self.subTest(path=path):
                after=copy.deepcopy(before);parent=after
                for key in path[:-1]:parent=parent[key]
                parent[path[-1]]=default
                result=compare(before,after);self.assertTrue(result['equivalent'])
                change=result['metadata_changes']['.'.join(path)]
                self.assertFalse(change['before_present']);self.assertTrue(change['after_present'])
                self.assertTrue(equivalent(after,before))
                for value in ({'requireApproval':True},True,[],None,0,''):
                    if value==default and type(value) is type(default):continue
                    parent[path[-1]]=value;self.assertFalse(equivalent(before,after),value)

    def test_api_defaults_are_scoped_to_their_resource_type(self):
        agent=copy.deepcopy(self.before)
        self.assertFalse(equivalent(agent,{**agent,'changeRequestConfig':{}}))
        binding={'name':'projects/participant-project/locations/us/gibqObservabilitySettings/ec-workshop-x/settingBindings/workshop-project','createTime':'c'}
        self.assertFalse(equivalent(binding,{**binding,'conversationalAnalyticsSetting':{'bqaaEnabled':False}}))
        group={'name':'projects/participant-project/locations/us/entryGroups/ec-lab1-x','createTime':'c'}
        self.assertFalse(equivalent(group,{**group,'bqaaEnabled':False}))
