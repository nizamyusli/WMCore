#!/usr/bin/env python
"""
_ReportIntegration_t_

Verify that the whole FWJR chain works correctly:
  CMSSW XML -> XMLParser -> Report -> Pickle -> UnPickle -> Accountant
"""

__revision__ = "$Id: ReportIntegration_t.py,v 1.2 2010/03/30 20:48:12 sfoulkes Exp $"
__version__ = "$Revision: 1.2 $"

import unittest
import os
import xml.dom.minidom
import tempfile
import threading

import WMCore.WMInit
from WMQuality.TestInit import TestInit
from WMCore.DAOFactory import DAOFactory

from WMCore.WMBS.File import File
from WMCore.WMBS.Fileset import Fileset
from WMCore.WMBS.Job import Job
from WMCore.WMBS.JobGroup import JobGroup
from WMCore.WMBS.Subscription import Subscription
from WMCore.WMBS.Workflow import Workflow

from WMComponent.JobAccountant.JobAccountantPoller import JobAccountantPoller
from WMCore.FwkJobReport.Report import Report

def requiresPython26(testMethod, *args, **kwargs):
    def skipTest(*args, **kwargs):
        print "SKIPPING"
        #raise nose.SkipTest
        
    import sys

    majorVersion = sys.version_info[0]
    minorVersion = sys.version_info[1]

    if (majorVersion == 2 and minorVersion >= 6) or majorVersion > 2:
        return testMethod

    return skipTest

class ReportIntegrationTest(unittest.TestCase):
    """
    _ReportIntegrationTest_

    """
    def setUp(self):
        """
        _setUp_

        Setup the database and WMBS for the test.
        """
        self.testInit = TestInit(__file__)
        self.testInit.setLogging()
        self.testInit.setDatabaseConnection()
        self.testInit.setSchema(customModules = ["WMComponent.DBSBuffer.Database",
                                                 "WMCore.WMBS"],
                                useDefault = False)

        inputFile = File(lfn = "/path/to/some/lfn", size = 10, events = 10,
                         locations = "cmssrm.fnal.gov")
        inputFile.create()
                             
        inputFileset = Fileset(name = "InputFileset")
        inputFileset.create()
        inputFileset.addFile(inputFile)
        inputFileset.commit()

        unmergedFileset = Fileset(name = "UnmergedFileset")
        unmergedFileset.create()

        mergedFileset = Fileset(name = "MergedFileset")
        mergedFileset.create()
        
        procWorkflow = Workflow(spec = "wf001.xml", owner = "Steve",
                                name = "TestWF", task = "None")
        procWorkflow.create()
        procWorkflow.addOutput("outputRECORECO", unmergedFileset)

        mergeWorkflow = Workflow(spec = "wf002.xml", owner = "Steve",
                                 name = "MergeWF", task = "None")
        mergeWorkflow.create()
        mergeWorkflow.addOutput("Merged", mergedFileset)
        
        self.procSubscription = Subscription(fileset = inputFileset,
                                             workflow = procWorkflow,
                                             split_algo = "FileBased",
                                             type = "Processing")
        self.procSubscription.create()
        self.procSubscription.acquireFiles()

        self.mergeSubscription = Subscription(fileset = unmergedFileset,
                                             workflow = mergeWorkflow,
                                             split_algo = "WMBSMergeBySize",
                                             type = "Merge")
        self.mergeSubscription.create()

        self.procJobGroup = JobGroup(subscription = self.procSubscription)
        self.procJobGroup.create()
        self.mergeJobGroup = JobGroup(subscription = self.mergeSubscription)
        self.mergeJobGroup.create()        

        self.testJob = Job(name = "testJob", files = [inputFile])
        self.testJob.create(group = self.procJobGroup)
        self.testJob["state"] = "complete"

        myThread = threading.currentThread()
        self.daofactory = DAOFactory(package = "WMCore.WMBS",
                                     logger = myThread.logger,
                                     dbinterface = myThread.dbi)
        self.stateChangeAction = self.daofactory(classname = "Jobs.ChangeState")
        self.setFWJRAction = self.daofactory(classname = "Jobs.SetFWJRPath")
        self.getJobTypeAction = self.daofactory(classname = "Jobs.GetType")
        locationAction = self.daofactory(classname = "Locations.New")
        locationAction.execute(siteName = "cmssrm.fnal.gov")

        self.stateChangeAction.execute(jobs = [self.testJob])

        self.tempDir = tempfile.mkdtemp()
        return

    def tearDown(self):
        """
        _tearDown_

        Clear out the database and the pickled report file.
        """
        self.testInit.clearDatabase()

        try:
            os.remove(os.path.join(self.tempDir, "ProcReport.pkl"))
            os.remove(os.path.join(self.tempDir, "MergeReport.pkl"))            
        except Exception, ex:
            pass

        try:
            os.rmdir(self.tempDir)
        except Exception, ex:
            pass
        
        return

    def createConfig(self, workerThreads):
        """
        _createConfig_

        Create a config for the JobAccountant with the given number of worker
        threads.  This config needs to include information for connecting to the
        database as the component will create it's own database connections.
        These parameters are still pulled from the environment.
        """
        config = self.testInit.getConfiguration()
        self.testInit.generateWorkDir(config)

        config.section_("JobStateMachine")
        config.JobStateMachine.couchurl = os.getenv("COUCHURL")
        config.JobStateMachine.couchDBName = "report_integration_t"

        config.component_("JobAccountant")
        config.JobAccountant.pollInterval = 60
        config.JobAccountant.workerThreads = workerThreads
        config.JobAccountant.componentDir = os.getcwd()
        config.JobAccountant.logLevel = 'SQLDEBUG'

        return config

    def verifyJobSuccess(self, jobID):
        """
        _verifyJobSuccess_

        Verify that the metadata for a successful job is correct.  This will
        check the outcome, retry count and state.
        """
        testJob = Job(id = jobID)
        testJob.load()

        assert testJob["state"] == "success", \
               "Error: test job in wrong state: %s" % testJob["state"]
        assert testJob["retry_count"] == 0, \
               "Error: test job has wrong retry count: %s" % testJob["retry_count"]
        assert testJob["outcome"] == "success", \
               "Error: test job has wrong outcome: %s" % testJob["outcome"]

        return

    def verifyFileMetaData(self, jobID, fwkJobReportFiles):
        """
        _verifyFileMetaData_

        Verify that all the files that were output by a job made it into WMBS
        correctly.  Compare the contents of WMBS to the files in the frameworks
        job report.

        Note that fwkJobReportFiles is a list of DataStructs File objects.
        """
        testJob = Job(id = jobID)
        testJob.loadData()

        inputLFNs = []
        for inputFile in testJob["input_files"]:
            inputLFNs.append(inputFile["lfn"])

        for fwkJobReportFile in fwkJobReportFiles:
            outputFile = File(lfn = fwkJobReportFile["lfn"])
            outputFile.loadData(parentage = 1)

            assert outputFile["events"] == int(fwkJobReportFile["events"]), \
                   "Error: Output file has wrong events: %s, %s" % \
                   (outputFile["events"], fwkJobReportFile["events"])
            assert outputFile["size"] == int(fwkJobReportFile["size"]), \
                   "Error: Output file has wrong size: %s, %s" % \
                   (outputFile["size"], fwkJobReportFile["size"])

            for ckType in fwkJobReportFile["checksums"].keys():
                assert ckType in outputFile["checksums"].keys(), \
                       "Error: Output file is missing checksums: %s" % ckType
                assert outputFile["checksums"][ckType] == fwkJobReportFile["checksums"][ckType], \
                       "Error: Checksums don't match."
                       
            assert len(fwkJobReportFile["checksums"].keys()) == \
                   len(outputFile["checksums"].keys()), \
                   "Error: Wrong number of checksums."

            jobType = self.getJobTypeAction.execute(jobID = jobID)
            if jobType == "Merge":
                assert str(outputFile["merged"]) == "True", \
                       "Error: Merge jobs should output merged files."
            else:
                assert outputFile["merged"] == fwkJobReportFile["merged"], \
                       "Error: Output file merged output is wrong: %s, %s" % \
                       (outputFile["merged"], fwkJobReportFile["merged"])            

            assert len(outputFile["locations"]) == 1, \
                   "Error: outputfile should have one location: %s" % outputFile["locations"]
            assert list(outputFile["locations"])[0] == list(fwkJobReportFile["locations"])[0], \
                   "Error: wrong location for file."

            assert len(outputFile["parents"]) == len(inputLFNs), \
                   "Error: Output file has wrong number of parents."
            for outputParent in outputFile["parents"]:
                assert outputParent["lfn"] in inputLFNs, \
                       "Error: Unknown parent file: %s" % outputParent["lfn"]

            fwjrRuns = {}
            for run in fwkJobReportFile["runs"]:
                fwjrRuns[run.run] = run.lumis

            for run in outputFile["runs"]:
                assert fwjrRuns.has_key(run.run), \
                       "Error: Extra run in output: %s" % run.run

                for lumi in run:
                    assert lumi in fwjrRuns[run.run], \
                           "Error: Extra lumi: %s" % lumi
                    
                    fwjrRuns[run.run].remove(lumi)

                if len(fwjrRuns[run.run]) == 0:
                        del fwjrRuns[run.run]
                        
            assert len(fwjrRuns.keys()) == 0, \
                   "Error: Missing runs, lumis: %s" % fwjrRuns

            testJobGroup = JobGroup(id = testJob["jobgroup"])
            testJobGroup.loadData()
            jobGroupFileset = testJobGroup.output
            jobGroupFileset.loadData()

            assert outputFile["id"] in jobGroupFileset.getFiles(type = "id"), \
                   "Error: output file not in jobgroup fileset."

            if testJob["mask"]["FirstEvent"] == None:
                assert outputFile["first_event"] == 0, \
                       "Error: first event not set correctly: 0, %s" % \
                       outputFile["first_event"]
            else:
                assert testJob["mask"]["FirstEvent"] == outputFile["first_event"], \
                       "Error: last event not set correctly: %s, %s" % \
                       (testJob["mask"]["FirstEvent"], outputFile["first_event"])

            if testJob["mask"]["LastEvent"] == None:
                assert outputFile["last_event"] == int(fwkJobReportFile["events"]), \
                       "Error: last event not set correctly: %s, %s" % \
                       (fwkJobReportFile["TotalEvents"], outputFile["last_event"])
            else:
                assert testJob["mask"]["LastEvent"] == outputFile["last_event"], \
                       "Error: last event not set correctly: %s, %s" % \
                       (testJob["mask"]["LastEvent"], outputFile["last_event"])                   

        return

    @requiresPython26
    def testReportHandling(self):
        """
        _testReportHandling_

        Verify that we're able to parse a CMSSW report, convert it to a Report()
        style report, pickle it and then have the accountant process it.
        """
        self.procPath = os.path.join(WMCore.WMInit.getWMBASE(),
                                    "test/python/WMCore_t/FwkJobReport_t/CMSSWProcessingReport.xml")
        
        myReport = Report("cmsRun1")
        myReport.parse(self.procPath)

        # Fake some metadata that should be added by the stageout scripts.
        for fileRef in myReport.getAllFileRefsFromStep("cmsRun1"):
            fileRef.size = 1024
            fileRef.location = "cmssrm.fnal.gov"

        fwjrPath = os.path.join(self.tempDir, "ProcReport.pkl")
        myReport.persist(fwjrPath)

        self.setFWJRAction.execute(jobID = self.testJob["id"], fwjrPath = fwjrPath)

        config = self.createConfig(workerThreads = 1)
        accountant = JobAccountantPoller(config)
        accountant.setup()
        accountant.algorithm()

        self.verifyJobSuccess(self.testJob["id"])
        self.verifyFileMetaData(self.testJob["id"], myReport.getAllFilesFromStep("cmsRun1"))

        inputFile = File(lfn = "/store/backfill/2/unmerged/WMAgentCommissioining10/MinimumBias/RECO/rereco_GR09_R_34X_V5_All_v1/0000/outputRECORECO.root")
        inputFile.load()
        self.testMergeJob = Job(name = "testMergeJob", files = [inputFile])
        self.testMergeJob.create(group = self.mergeJobGroup)
        self.testMergeJob["state"] = "complete"
        self.stateChangeAction.execute(jobs = [self.testMergeJob])

        self.mergePath = os.path.join(WMCore.WMInit.getWMBASE(),
                                         "test/python/WMCore_t/FwkJobReport_t/CMSSWMergeReport.xml")
        
        myReport = Report("mergeReco")
        myReport.parse(self.mergePath)

        # Fake some metadata that should be added by the stageout scripts.
        for fileRef in myReport.getAllFileRefsFromStep("mergeReco"):
            fileRef.size = 1024
            fileRef.location = "cmssrm.fnal.gov"
            fileRef.dataset = {"applicationName": "cmsRun", "applicationVersion": "CMSSW_3_4_2_patch1",
                               "primaryDataset": "MinimumBias", "processedDataset": "Rereco-v1",
                               "dataTier": "RECO"}

        fwjrPath = os.path.join(self.tempDir, "MergeReport.pkl")
        myReport.persist(fwjrPath)

        self.setFWJRAction.execute(jobID = self.testMergeJob["id"], fwjrPath = fwjrPath)
        accountant.algorithm()

        self.verifyJobSuccess(self.testMergeJob["id"])
        self.verifyFileMetaData(self.testMergeJob["id"], myReport.getAllFilesFromStep("mergeReco"))

        return
        
if __name__ == "__main__":
    unittest.main()
