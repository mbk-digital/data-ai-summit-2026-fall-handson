import contextlib
import copy
import io
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from workshop.cloud import CloudError
from workshop.observability import Observability, BASE


class ObservabilityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.patch = patch('workshop.observability.ROOT',Path(self.temp.name));self.patch.start();self.addCleanup(self.patch.stop)
        self.o = Observability({'project_id':'test-project','dataset_id':'demo','bigquery_location':'US'})
        self.binding = self.o.name+'/settingBindings/workshop-project'
        self.resources={self.o.name:{'name':self.o.name,'setting':True},self.binding:{'name':self.binding,'target':'projects/test-project'}}
        self.o.ledger={n:{'state':'present','snapshot':copy.deepcopy(v)} for n,v in self.resources.items()}
        self.deleted=[]
        def request(method,url,payload=None,missing_ok=False):
            name=url.removeprefix(BASE)
            if name.endswith('/settingBindings'):
                return {'settingBindings':[v for n,v in self.resources.items() if '/settingBindings/' in n]}
            if method=='DELETE':self.deleted.append(name);self.resources.pop(name);return {}
            return copy.deepcopy(self.resources.get(name))
        self.o.c.request=request

    def test_setting_with_new_api_default_is_reused_but_enabled_value_is_refused(self):
        setting={'name':self.o.name,'createTime':'c','conversationalAnalyticsSetting':{'metricsEnabled':True,'tracesEnabled':True}}
        self.o.ledger[self.o.name]={'state':'present','snapshot':copy.deepcopy(setting)}
        for bqaa,ok in ((False,True),(True,False)):
            with self.subTest(bqaa=bqaa):
                self.resources[self.o.name]=copy.deepcopy(setting);self.resources[self.o.name]['conversationalAnalyticsSetting']['bqaaEnabled']=bqaa
                if ok:self.assertEqual(self.o.ensure(self.o.name,self.o.collection,{},{}),self.resources[self.o.name])
                else:
                    with self.assertRaises(CloudError):self.o.ensure(self.o.name,self.o.collection,{},{})

    def test_plan_does_not_mutate(self):
        with contextlib.redirect_stdout(io.StringIO()):self.o.cleanup(False)
        self.assertFalse(self.deleted)

    def test_binding_deleted_first(self):
        with contextlib.redirect_stdout(io.StringIO()):self.o.cleanup(True)
        self.assertEqual(self.deleted,[self.binding,self.o.name])
        with contextlib.redirect_stdout(io.StringIO()):self.o.cleanup(True)
        self.assertEqual(len(self.deleted),2)

    def test_changed_setting_refused_before_deletion(self):
        self.resources[self.o.name]['setting']=False
        with self.assertRaises(CloudError):self.o.cleanup(True)
        self.assertFalse(self.deleted)

    def test_foreign_binding_refused_before_deletion(self):
        n=self.o.name+'/settingBindings/foreign';self.resources[n]={'name':n}
        with self.assertRaises(CloudError):self.o.cleanup(True)
        self.assertFalse(self.deleted)

    def test_unconfirmed_create_refused(self):
        self.o.ledger[self.binding]['state']='requested'
        with self.assertRaises(CloudError):self.o.cleanup(True)
        self.assertFalse(self.deleted)
