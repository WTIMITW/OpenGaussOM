# -*- coding:utf-8 -*-
#############################################################################
# Copyright (c) 2020 Huawei Technologies Co.,Ltd.
#
# openGauss is licensed under Mulan PSL v2.
# You can use this software according to the terms
# and conditions of the Mulan PSL v2.
# You may obtain a copy of Mulan PSL v2 at:
#
#          http://license.coscl.org.cn/MulanPSL2
#
# THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS,
# WITHOUT WARRANTIES OF ANY KIND,
# EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
# MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
# See the Mulan PSL v2 for more details.
# ----------------------------------------------------------------------------
# Description  : ExpansionImpl.py
#############################################################################

import subprocess
import sys
import re
import os
import getpass
import pwd
import datetime
import weakref
from random import sample
import time
import grp
import socket
import stat
from multiprocessing import Process, Value

sys.path.append(sys.path[0] + "/../../../../")
from gspylib.common.DbClusterInfo import dbClusterInfo, queryCmd
from gspylib.threads.SshTool import SshTool
from gspylib.common.DbClusterStatus import DbClusterStatus
from gspylib.common.ErrorCode import ErrorCode
from gspylib.common.Common import DefaultValue
from gspylib.common.GaussLog import GaussLog

sys.path.append(sys.path[0] + "/../../../lib/")
DefaultValue.doConfigForParamiko()
import paramiko


#boot/build mode
MODE_PRIMARY = "primary"
MODE_STANDBY = "standby"
MODE_NORMAL = "normal"
MODE_CASCADE = "cascade_standby"

# instance local_role
ROLE_NORMAL = "normal"
ROLE_PRIMARY = "primary"
ROLE_STANDBY = "standby"
ROLE_CASCADE = "cascade standby"

#db state
STAT_NORMAL = "normal"

# master 
MASTER_INSTANCE = 0
# standby 
STANDBY_INSTANCE = 1

# statu failed
STATUS_FAIL = "Failure"

class ExpansionImpl():
    """
    class for expansion standby node.
    step:
        1. preinstall database on new standby node
        2. install as single-node database
        3. establish primary-standby relationship of all node
    """

    def __init__(self, expansion):
        """
        """
        self.context = expansion

        self.user = self.context.user
        self.group = self.context.group
        self.existingHosts = []
        self.expansionSuccess = {}
        self.logger = self.context.logger

        envFile = DefaultValue.getEnv("MPPDB_ENV_SEPARATE_PATH")
        if envFile:
            self.envFile = envFile
        else:
            self.envFile = "/etc/profile"

        currentTime = str(datetime.datetime.now()).replace(" ", "_").replace(
            ".", "_")

        self.commonGsCtl = GsCtlCommon(expansion)
        self.tempFileDir = "/tmp/gs_expansion_%s" % (currentTime)
        self.logger.debug("tmp expansion dir is %s ." % self.tempFileDir)

        self._finalizer = weakref.finalize(self, self.clearTmpFile)

    def sendSoftToHosts(self):
        """
        create software dir and send it on each nodes
        """
        self.logger.debug("Start to send soft to each standby nodes.\n")
        hostNames = self.context.newHostList
        hostList = hostNames

        sshTool = SshTool(hostNames)

        srcFile = self.context.packagepath
        targetDir = os.path.realpath(
            os.path.join(srcFile, "../"))

        ## mkdir package dir and send package to remote nodes.
        sshTool.executeCommand("mkdir -p %s" % srcFile , "", 
            DefaultValue.SUCCESS, hostList)
        sshTool.scpFiles(srcFile, targetDir, hostList)

        ## change mode of package dir to set privileges for users
        tPathList = os.path.split(targetDir)
        path2ChangeMode = targetDir
        if len(tPathList) > 2:
            path2ChangeMode = os.path.join(tPathList[0],tPathList[1])
        changeModCmd =  "chmod -R a+x {srcFile}".format(user=self.user,
            group=self.group,srcFile=path2ChangeMode)
        sshTool.executeCommand(changeModCmd, "", DefaultValue.SUCCESS,
                       hostList)
        self.logger.debug("End to send soft to each standby nodes.\n")
        self.cleanSshToolFile(sshTool)

    def generateAndSendXmlFile(self):
        """
        """
        self.logger.debug("Start to generateAndSend XML file.\n")

        tempXmlFile = "%s/clusterconfig.xml" % self.tempFileDir
        cmd = "mkdir -p %s; touch %s; cat /dev/null > %s" % \
        (self.tempFileDir, tempXmlFile, tempXmlFile)
        (status, output) = subprocess.getstatusoutput(cmd)

        cmd = "chown -R %s:%s %s" % (self.user, self.group, self.tempFileDir)
        (status, output) = subprocess.getstatusoutput(cmd)
        
        newHosts = self.context.newHostList
        for host in newHosts:
            # create single deploy xml file for each standby node
            xmlContent = self.__generateXml(host)
            with os.fdopen(os.open("%s" % tempXmlFile, os.O_WRONLY | os.O_CREAT,
             stat.S_IWUSR | stat.S_IRUSR),'w') as fo:
                fo.write( xmlContent )
                fo.close()
            # send single deploy xml file to each standby node
            sshTool = SshTool(host)
            retmap, output = sshTool.getSshStatusOutput("mkdir -p %s" % 
            self.tempFileDir , [host], self.envFile)
            retmap, output = sshTool.getSshStatusOutput("chown %s:%s %s" % 
            (self.user, self.group, self.tempFileDir), [host], self.envFile)
            sshTool.scpFiles("%s" % tempXmlFile, "%s" % 
            tempXmlFile, [host], self.envFile)
            self.cleanSshToolFile(sshTool)
        
        self.logger.debug("End to generateAndSend XML file.\n")

    def __generateXml(self, backIp):
        """
        """
        nodeName = self.context.backIpNameMap[backIp]
        nodeInfo = self.context.clusterInfoDict[nodeName]

        backIp = nodeInfo["backIp"]
        sshIp = nodeInfo["sshIp"]
        port = nodeInfo["port"]
        dataNode = nodeInfo["dataNode"]

        appPath = self.context.clusterInfoDict["appPath"]
        logPath = self.context.clusterInfoDict["logPath"]
        corePath = self.context.clusterInfoDict["corePath"]
        toolPath = self.context.clusterInfoDict["toolPath"]
        mppdbconfig = ""
        tmpMppdbPath = DefaultValue.getEnv("PGHOST")
        if tmpMppdbPath:
            mppdbconfig = '<PARAM name="tmpMppdbPath" value="%s" />' % tmpMppdbPath
        azName = self.context.hostAzNameMap[backIp]

        xmlConfig = """\
<?xml version="1.0" encoding="UTF-8"?>
<ROOT>
    <CLUSTER>
        <PARAM name="clusterName" value="dbCluster" />
        <PARAM name="nodeNames" value="{nodeName}" />
        <PARAM name="backIp1s" value="{backIp}"/>
        <PARAM name="gaussdbAppPath" value="{appPath}" />
        <PARAM name="gaussdbLogPath" value="{logPath}" />
        <PARAM name="gaussdbToolPath" value="{toolPath}" />
        {mappdbConfig}
        <PARAM name="corePath" value="{corePath}"/>
        <PARAM name="clusterType" value="single-inst"/>
    </CLUSTER>
    <DEVICELIST>
        <DEVICE sn="1000001">
            <PARAM name="name" value="{nodeName}"/>
            <PARAM name="azName" value="{azName}"/>
            <PARAM name="azPriority" value="1"/>
            <PARAM name="backIp1" value="{backIp}"/>
            <PARAM name="sshIp1" value="{sshIp}"/>
            <!--dbnode-->
            <PARAM name="dataNum" value="1"/>
            <PARAM name="dataPortBase" value="{port}"/>
            <PARAM name="dataNode1" value="{dataNode}"/>
        </DEVICE>
    </DEVICELIST>
</ROOT>
        """.format(nodeName=nodeName,backIp=backIp,appPath=appPath,
        logPath=logPath,toolPath=toolPath,corePath=corePath,
        sshIp=sshIp,port=port,dataNode=dataNode,azName=azName,
        mappdbConfig=mppdbconfig)
        return xmlConfig

    def changeUser(self):
        user = self.user
        try:
            pw_record = pwd.getpwnam(user)
        except Exception:
            GaussLog.exitWithError(ErrorCode.GAUSS_503["GAUSS_50300"] % user)

        user_name = pw_record.pw_name
        user_uid = pw_record.pw_uid
        user_gid = pw_record.pw_gid
        os.setgid(user_gid)
        os.setuid(user_uid)
        os.environ["HOME"] = pw_record.pw_dir
        os.environ["USER"] = user_name
        os.environ["LOGNAME"] = user_name
        os.environ["SHELL"] = pw_record.pw_shell


    def initSshConnect(self, host, user='root'):
        
        try:
            getPwdStr = "Please enter the password of user [%s] on node [%s]: " \
             % (user, host)
            passwd = getpass.getpass(getPwdStr)
            self.sshClient = paramiko.SSHClient()
            self.sshClient.set_missing_host_key_policy(paramiko.AutoAddPolicy())
            self.sshClient.connect(host, 22, user, passwd)
        except paramiko.ssh_exception.AuthenticationException as e :
            self.logger.log("Authentication failed.")
            self.initSshConnect(host, user)

    def installDatabaseOnHosts(self):
        """
        install database on each standby node
        """
        hostList = self.context.newHostList
        envfile = self.envFile
        tempXmlFile = "%s/clusterconfig.xml" % self.tempFileDir
        installCmd = "source {envfile} ; gs_install -X {xmlfile} \
            2>&1".format(envfile=envfile,xmlfile=tempXmlFile)

        statusArr = []

        for newHost in hostList:

            self.logger.log("\ninstalling database on node %s:" % newHost)
            self.logger.debug(installCmd)

            hostName = self.context.backIpNameMap[newHost]
            sshIp = self.context.clusterInfoDict[hostName]["sshIp"]
            self.initSshConnect(sshIp, self.user)

            stdin, stdout, stderr = self.sshClient.exec_command(installCmd, 
            get_pty=True)
            channel = stdout.channel
            echannel = stderr.channel

            while not channel.exit_status_ready():
                try:
                    recvOut = channel.recv(1024)
                    outDecode = recvOut.decode("utf-8")
                    outStr = outDecode.strip()
                    if(len(outStr) == 0):
                        continue
                    if(outDecode.endswith("\r\n")):
                        self.logger.log(outStr)
                    else:
                        value = ""
                        if re.match(r".*yes.*no.*", outStr):
                            value = input(outStr)
                            while True:
                                # check the input
                                if (
                                    value.upper() != "YES"
                                    and value.upper() != "NO"
                                    and value.upper() != "Y"
                                    and value.upper() != "N"):
                                    value = input("Please type 'yes' or 'no': ")
                                    continue
                                break
                        else:
                            value = getpass.getpass(outStr)
                        stdin.channel.send("%s\r\n" %value)
                        stdin.flush()
                    stdout.flush()
                except Exception as e:
                    sys.exit(1)
                    pass
                if channel.exit_status_ready() and  \
                    not channel.recv_stderr_ready() and \
                    not channel.recv_ready(): 
                    channel.close()
                    break
            
            stdout.close()
            stderr.close()
            status = channel.recv_exit_status()
            statusArr.append(status)
        
        isBothSuccess = True
        for status in statusArr:
            if status != 0:
                isBothSuccess = False
                break
        if isBothSuccess:
            self.logger.log("\nSuccessfully install database on node %s" %
             hostList)
        else:
            sys.exit(1)
    
    def preInstallOnHosts(self):
        """
        execute preinstall step
        """
        self.logger.debug("Start to preinstall database step.\n")
        newBackIps = self.context.newHostList
        newHostNames = []
        for host in newBackIps:
            newHostNames.append(self.context.backIpNameMap[host])
        envfile = self.envFile
        tempXmlFile = "%s/clusterconfig.xml" % self.tempFileDir

        if envfile == "/etc/profile":
            preinstallCmd = "{softpath}/script/gs_preinstall -U {user} -G {group} \
            -X {xmlfile} --non-interactive 2>&1\
                    ".format(softpath=self.context.packagepath,user=self.user,
                    group=self.group,xmlfile=tempXmlFile)
        else:
            preinstallCmd = "{softpath}/script/gs_preinstall -U {user} -G {group} \
                -X {xmlfile} --sep-env-file={envfile} \
                --non-interactive 2>&1\
                    ".format(softpath=self.context.packagepath,user=self.user,
                    group=self.group,xmlfile=tempXmlFile,envfile=envfile)

        sshTool = SshTool(newHostNames)
        
        status, output = sshTool.getSshStatusOutput(preinstallCmd , [], envfile)
        statusValues = status.values()
        if STATUS_FAIL in statusValues:
            GaussLog.exitWithError(output)
        
        self.logger.debug("End to preinstall database step.\n")
        self.cleanSshToolFile(sshTool)

    
    def buildStandbyRelation(self):
        """
        func: after install single database on standby nodes. 
        build the relation with primary and standby nodes.
        step:
        1. get existing hosts
        2. set guc config to primary node
        3. restart standby node with Standby Mode
        4. set guc config to standby node
        5. rollback guc config of existing hosts if build failed
        6. generate cluster static file and send to each node.
        """
        self.getExistingHosts()
        self.setPrimaryGUCConfig()
        self.setStandbyGUCConfig()
        self.addTrustOnExistNodes()
        self.generateGRPCCert()
        self.buildStandbyHosts()
        self.rollback()
        self.generateClusterStaticFile()

    def getExistingHosts(self):
        """
        get the exiting hosts
        """
        self.logger.debug("Get the existing hosts.\n")
        primaryHost = self.getPrimaryHostName()
        result = self.commonGsCtl.queryOmCluster(primaryHost, self.envFile)
        instances = re.split('(?:\|)|(?:\n)', result)
        self.existingHosts = []
        for inst in instances:
            pattern = re.compile('(\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}).*')
            result = pattern.findall(inst)
            if len(result) != 0:
                self.existingHosts.append(result[0])

    def setPrimaryGUCConfig(self):
        """
        """
        self.logger.debug("Start to set primary node GUC config.\n")
        primaryHost = self.getPrimaryHostName()

        self.setGUCOnClusterHosts([primaryHost])
        self.addStandbyIpInPrimaryConf()
        
        
    def setStandbyGUCConfig(self):
        """
        set the expansion standby node db guc config
        """
        self.logger.debug("Start to set standby node GUC config.\n")
        primaryHost = self.getPrimaryHostName()
        existingStandbyHosts = list(
            set(self.existingHosts).difference(set([primaryHost])))
        standbyHosts = existingStandbyHosts + self.context.newHostList
        standbyNames = []
        for standbyHost in standbyHosts:
            standbyNames.append(self.context.backIpNameMap[standbyHost])
        self.setGUCOnClusterHosts(standbyNames)

    def addTrustOnExistNodes(self):
        """
        add host trust in pg_hba.conf on existing standby node. 
        """ 
        self.logger.debug("Start to set host trust on existing node.")
        allNodeNames = self.context.nodeNameList
        newNodeIps = self.context.newHostList
        newNodeNames = []
        trustCmd = []
        for node in newNodeIps:
            nodeName = self.context.backIpNameMap[node]
            newNodeNames.append(nodeName)
            cmd = 'host    all    all    %s/32    trust' % node
            trustCmd.append(cmd)
        existNodes = list(set(allNodeNames).difference(set(newNodeNames)))
        for node in existNodes:
            dataNode = self.context.clusterInfoDict[node]["dataNode"]
            cmd = ""
            for trust in trustCmd:
                cmd += "source %s; gs_guc set -D %s -h '%s';" % \
                    (self.envFile, dataNode, trust)
            sshTool = SshTool([node])
            resultMap, outputCollect = sshTool.getSshStatusOutput(cmd, 
            [node], self.envFile)
            self.cleanSshToolFile(sshTool)
        self.logger.debug("End to set host trust on existing node.")
    
    def generateGRPCCert(self):
        """
        generate GRPC cert
        """
        primaryHost = self.getPrimaryHostName()
        dataNode = self.context.clusterInfoDict[primaryHost]["dataNode"]
        insType, dbStat = self.commonGsCtl.queryInstanceStatus(primaryHost,
            dataNode,self.envFile)
        needGRPCHosts = self.context.newHostList
        if insType != MODE_PRIMARY:
            primaryHostIp = self.context.clusterInfoDict[primaryHost]["backIp"]
            needGRPCHosts.append(primaryHostIp)
        self.logger.debug("\nStart to generate GRPC cert.")
        self.context.initSshTool(needGRPCHosts)
        self.context.createGrpcCa(needGRPCHosts)
        self.logger.debug("\nEnd to generate GRPC cert.")

    def addStandbyIpInPrimaryConf(self):
        """
        add standby hosts ip in primary node pg_hba.conf
        """

        standbyHosts = self.context.newHostList
        primaryHost = self.getPrimaryHostName()
        command = ''
        for host in standbyHosts:
            hostName = self.context.backIpNameMap[host]
            dataNode = self.context.clusterInfoDict[hostName]["dataNode"]
            command += ("source %s; gs_guc set -D %s -h 'host    all    all    %s/32    " + \
                "trust';") % (self.envFile, dataNode, host)
        self.logger.debug(command)
        sshTool = SshTool([primaryHost])
        resultMap, outputCollect = sshTool.getSshStatusOutput(command, 
        [primaryHost], self.envFile)
        self.logger.debug(outputCollect)
        self.cleanSshToolFile(sshTool)

    def reloadPrimaryConf(self):
        """
        """
        primaryHost = self.getPrimaryHostName()
        dataNode = self.context.clusterInfoDict[primaryHost]["dataNode"]
        command = "gs_ctl reload -D %s " % dataNode
        sshTool = SshTool([primaryHost])
        self.logger.debug(command)
        resultMap, outputCollect = sshTool.getSshStatusOutput(command, 
            [primaryHost], self.envFile)
        self.logger.debug(outputCollect)
        self.cleanSshToolFile(sshTool)

    def getPrimaryHostName(self):
        """
        """
        primaryHost = ""
        for nodeName in self.context.nodeNameList:
            if self.context.clusterInfoDict[nodeName]["instanceType"] \
                    == MASTER_INSTANCE:
                primaryHost = nodeName
                break
        return primaryHost


    def buildStandbyHosts(self):
        """
        stop the new standby host`s database and build it as standby mode
        """
        self.logger.debug("start to build standby node...\n")
        
        standbyHosts = self.context.newHostList
        primaryHost = self.getPrimaryHostName()
        existingStandbys = list(set(self.existingHosts).difference(set([primaryHost])))

        for host in standbyHosts:
            self.expansionSuccess[host] = False

        # build standby firstly
        for host in standbyHosts:
            if self.context.newHostCasRoleMap[host] == "on":
                continue
            self.logger.log("Start to build standby %s." % host)
            startSuccess = False
            hostName = self.context.backIpNameMap[host]
            dataNode = self.context.clusterInfoDict[hostName]["dataNode"]

            self.checkTmpDir(hostName)

            self.commonGsCtl.stopInstance(hostName, dataNode, self.envFile)
            self.commonGsCtl.startInstanceWithMode(hostName, dataNode, 
                MODE_STANDBY, self.envFile)
            
            # start standby as standby mode for three times max.
            start_retry_num = 1
            while start_retry_num <= 3:
                insType, dbStat = self.commonGsCtl.queryInstanceStatus(
                    hostName,dataNode, self.envFile)
                if insType != ROLE_STANDBY:
                    self.logger.debug("Start database as Standby mode failed, "\
                        "retry for %s times" % start_retry_num)
                    self.commonGsCtl.startInstanceWithMode(hostName, dataNode, 
                    MODE_STANDBY, self.envFile)
                    start_retry_num = start_retry_num + 1
                else:
                    startSuccess = True
                    break
            if startSuccess == False:
                self.logger.debug("Start database %s as Standby mode failed!" % host)
                continue
            
            buildSuccess = False
            # build standby node
            self.addStandbyIpInPrimaryConf()
            self.reloadPrimaryConf()
            time.sleep(10)
            insType, dbStat = self.commonGsCtl.queryInstanceStatus( 
                primaryHost, dataNode, self.envFile)
            if insType != ROLE_PRIMARY:
                GaussLog.exitWithError("The server mode of primary host" \
                    "is not primary!")
            if dbStat != STAT_NORMAL:
                GaussLog.exitWithError("The primary is not Normal!")
            
            self.commonGsCtl.buildInstance(hostName, dataNode, MODE_STANDBY, 
                self.envFile)
            
            # if build failed first time. retry for three times.
            start_retry_num = 1
            while start_retry_num <= 3:
                time.sleep(10)
                insType, dbStat = self.commonGsCtl.queryInstanceStatus( 
                    hostName, dataNode, self.envFile)
                if dbStat != STAT_NORMAL:
                    self.logger.debug("Build standby instance failed, " \
                        "retry for %s times" % start_retry_num)
                    self.commonGsCtl.buildInstance(hostName, dataNode, 
                        MODE_STANDBY, self.envFile)
                    start_retry_num = start_retry_num + 1
                else:
                    buildSuccess = True
                    self.expansionSuccess[host] = True
                    existingStandbys.append(host)
                    break
            if buildSuccess == False:
                self.logger.log("Build standby %s failed." % host)
            else:
                self.logger.log("Build standby %s success." % host)


        # build cascade standby
        hostAzNameMap = self.context.hostAzNameMap
        for host in standbyHosts:
            if self.context.newHostCasRoleMap[host] == "off":
                continue
            self.logger.log("Start to build cascade standby %s." % host)
            startSuccess = False
            hostName = self.context.backIpNameMap[host]
            dataNode = self.context.clusterInfoDict[hostName]["dataNode"]
            # if no Normal standby same with the current cascade_standby, skip
            hasStandbyWithSameAZ = False
            for existingStandby in existingStandbys:
                existingStandbyName = self.context.backIpNameMap[existingStandby]
                existingStandbyDataNode = self.context.clusterInfoDict[existingStandbyName]["dataNode"]
                insType, dbStat = self.commonGsCtl.queryInstanceStatus( 
                    hostName, dataNode, self.envFile)
                if dbStat != STAT_NORMAL:
                    continue
                if hostAzNameMap[existingStandby] != hostAzNameMap[host]:
                    continue
                hasStandbyWithSameAZ = True
            if not hasStandbyWithSameAZ:
                self.logger.log("There is no Normal standby in %s" % \
                    hostAzNameMap[host])
                continue

            self.checkTmpDir(hostName)

            self.commonGsCtl.stopInstance(hostName, dataNode, self.envFile)
            self.commonGsCtl.startInstanceWithMode(hostName, dataNode, 
                MODE_STANDBY, self.envFile)
            
            # start cascadeStandby as standby mode for three times max.
            start_retry_num = 1
            while start_retry_num <= 3:
                insType, dbStat = self.commonGsCtl.queryInstanceStatus(hostName,
                    dataNode, self.envFile)
                if insType != ROLE_STANDBY:
                    self.logger.debug("Start database as Standby mode failed, "\
                        "retry for %s times" % start_retry_num)
                    self.commonGsCtl.startInstanceWithMode(hostName, dataNode, 
                        MODE_STANDBY, self.envFile)
                    start_retry_num = start_retry_num + 1
                else:
                    startSuccess = True
                    break
            if startSuccess == False:
                self.logger.log("Start database %s as Standby mode failed!" % host)
                continue

            # build cascade standby node
            self.addStandbyIpInPrimaryConf()
            self.reloadPrimaryConf()
            self.commonGsCtl.buildInstance(hostName, dataNode, MODE_CASCADE, \
                self.envFile)

            buildSuccess = False
            # if build failed first time. retry for three times.
            start_retry_num = 1
            while start_retry_num <= 3:
                time.sleep(10)
                insType, dbStat = self.commonGsCtl.queryInstanceStatus(
                    hostName, dataNode, self.envFile)
                if dbStat != STAT_NORMAL:
                    self.logger.debug("Build standby instance failed, "\
                        "retry for %s times" % start_retry_num)
                    self.addStandbyIpInPrimaryConf()
                    self.reloadPrimaryConf()
                    self.commonGsCtl.buildInstance(hostName, dataNode, \
                        MODE_CASCADE, self.envFile)
                    start_retry_num = start_retry_num + 1
                else:
                    buildSuccess = True
                    self.expansionSuccess[host] = True
                    break
            if buildSuccess == False:
                self.logger.log("Build cascade standby %s failed." % host)
            else:
                self.logger.log("Build cascade standby %s success." % host)
            

    def checkTmpDir(self, hostName):
        """
        if the tmp dir id not exist, create it.
        """
        tmpDir = os.path.realpath(DefaultValue.getTmpDirFromEnv())
        checkCmd = 'if [ ! -d "%s" ]; then exit 1;fi;' % (tmpDir)
        sshTool = SshTool([hostName])
        resultMap, outputCollect = sshTool.getSshStatusOutput(checkCmd, 
        [hostName], self.envFile)
        ret = resultMap[hostName]
        if ret == STATUS_FAIL:
            self.logger.debug("Node [%s] does not have tmp dir. need to fix.")
            fixCmd = "mkdir -p %s" % (tmpDir)
            sshTool.getSshStatusOutput(fixCmd, [hostName], self.envFile)
        self.cleanSshToolFile(sshTool)

    def generateClusterStaticFile(self):
        """
        generate static_config_files and send to all hosts
        """
        self.logger.log("Start to generate and send cluster static file.\n")

        primaryHost = self.getPrimaryHostName()
        result = self.commonGsCtl.queryOmCluster(primaryHost, self.envFile)
        for nodeName in self.context.nodeNameList:
            nodeInfo = self.context.clusterInfoDict[nodeName]
            nodeIp = nodeInfo["backIp"]
            dataNode = nodeInfo["dataNode"]
            exist_reg = r"(.*)%s[\s]*%s(.*)%s(.*)" % (nodeName, nodeIp, dataNode)
            dbNode = self.context.clusterInfo.getDbNodeByName(nodeName)
            if not re.search(exist_reg, result) and nodeIp not in self.context.newHostList:
                self.logger.debug("The node ip [%s] will not be added to cluster." % nodeIp)
                self.context.clusterInfo.dbNodes.remove(dbNode)
            if nodeIp in self.context.newHostList and not self.expansionSuccess[nodeIp]:
                self.context.clusterInfo.dbNodes.remove(dbNode)
        
        toolPath = self.context.clusterInfoDict["toolPath"]
        appPath = self.context.clusterInfoDict["appPath"]

        static_config_dir = "%s/script/static_config_files" % toolPath
        if not os.path.exists(static_config_dir):
            os.makedirs(static_config_dir)
        
        # valid if dynamic config file exists.
        dynamic_file = "%s/bin/cluster_dynamic_config" % appPath
        dynamic_file_exist = False
        if os.path.exists(dynamic_file):
            dynamic_file_exist = True
        
        for dbNode in self.context.clusterInfo.dbNodes:
            hostName = dbNode.name
            staticConfigPath = "%s/script/static_config_files/cluster_static_config_%s" % \
                (toolPath, hostName)
            self.context.clusterInfo.saveToStaticConfig(staticConfigPath, dbNode.id)
            srcFile = staticConfigPath
            if not os.path.exists(srcFile):
                GaussLog.exitWithError("Generate static file [%s] not found." % srcFile)
            hostSsh = SshTool([hostName])
            targetFile = "%s/bin/cluster_static_config" % appPath
            hostSsh.scpFiles(srcFile, targetFile, [hostName], self.envFile)
            # if dynamic config file exists, freshconfig it.
            if dynamic_file_exist:
                refresh_cmd = "gs_om -t refreshconf"
                hostSsh.getSshStatusOutput(refresh_cmd, [hostName], self.envFile)

            self.cleanSshToolFile(hostSsh)

        self.logger.debug("End to generate and send cluster static file.\n")
        
        self.logger.log("Expansion results:")
        for newHost in self.context.newHostList:
            if self.expansionSuccess[newHost]:
                self.logger.log("%s:\tSuccess" % nodeIp)
            else:
                self.logger.log("%s:\tFailed" % nodeIp)

    def setGUCOnClusterHosts(self, hostNames=[]):
        """
        guc config on all hosts 
        """

        gucDict = self.getGUCConfig()
        
        tempShFile = "%s/guc.sh" % self.tempFileDir

        if len(hostNames) == 0:
            hostNames = self.context.nodeNameList

        nodeDict = self.context.clusterInfoDict
        newHostList = self.context.newHostList
        hostAzNameMap = self.context.hostAzNameMap
        for host in hostNames:
            # set Available_zone for the new standby
            backIp = nodeDict[host]["backIp"]
            if backIp in newHostList:
                dataNode = nodeDict[host]["dataNode"]
                gucDict[host] += """\
gs_guc set -D {dn} -c "available_zone='{azName}'"
                """.format(dn = dataNode, azName = hostAzNameMap[backIp])
            command = "source %s ; " % self.envFile + gucDict[host]
            
            self.logger.debug(command)

            sshTool = SshTool([host])

            # create temporary dir to save guc command bashfile.
            mkdirCmd = "mkdir -m a+x -p %s; chown %s:%s %s" % \
                (self.tempFileDir,self.user,self.group,self.tempFileDir)
            retmap, output = sshTool.getSshStatusOutput(mkdirCmd, [host], \
                self.envFile)

            subprocess.getstatusoutput("touch %s; cat /dev/null > %s" % \
                (tempShFile, tempShFile))
            with os.fdopen(os.open("%s" % tempShFile, os.O_WRONLY | os.O_CREAT,
                    stat.S_IWUSR | stat.S_IRUSR),'w') as fo:
                fo.write("#bash\n")
                fo.write( command )
                fo.close()

            # send guc command bashfile to each host and execute it.
            sshTool.scpFiles("%s" % tempShFile, "%s" % tempShFile, [host], 
            self.envFile)
            
            resultMap, outputCollect = sshTool.getSshStatusOutput("sh %s" % \
                tempShFile, [host], self.envFile)

            self.logger.debug(outputCollect)
            self.cleanSshToolFile(sshTool)

    def getGUCConfig(self):
        """
        get guc config of each node:
            replconninfo[index]
        """
        nodeDict = self.context.clusterInfoDict
        hostNames = self.context.nodeNameList
        
        gucDict = {}

        for hostName in hostNames:
            
            localeHostInfo = nodeDict[hostName]
            index = 1
            guc_tempate_str = "source %s; " % self.envFile
            for remoteHost in hostNames:
                if(remoteHost == hostName):
                    continue
                remoteHostInfo = nodeDict[remoteHost]

                guc_repl_template = """\
gs_guc set -D {dn} -c "replconninfo{index}=\
'localhost={localhost} localport={localport} \
localheartbeatport={localeHeartPort} \
localservice={localservice} \
remotehost={remoteNode} \
remoteport={remotePort} \
remoteheartbeatport={remoteHeartPort} \
remoteservice={remoteservice}'"
                """.format(dn=localeHostInfo["dataNode"],
                index=index,
                localhost=localeHostInfo["sshIp"],
                localport=localeHostInfo["localport"],
                localeHeartPort=localeHostInfo["heartBeatPort"],
                localservice=localeHostInfo["localservice"],
                remoteNode=remoteHostInfo["sshIp"],
                remotePort=remoteHostInfo["localport"],
                remoteHeartPort=remoteHostInfo["heartBeatPort"],
                remoteservice=remoteHostInfo["localservice"])

                guc_tempate_str += guc_repl_template

                index += 1

            gucDict[hostName] = guc_tempate_str
        return gucDict

    def checkLocalModeOnStandbyHosts(self):
        """
        expansion the installed standby node. check standby database.
        1. if the database is normal
        2. if the databases version are same before existing and new 
        """
        standbyHosts = self.context.newHostList
        envfile = self.envFile
        
        self.logger.log("Checking the database with locale mode.")
        for host in standbyHosts:
            hostName = self.context.backIpNameMap[host]
            dataNode = self.context.clusterInfoDict[hostName]["dataNode"]
            insType, dbStat = self.commonGsCtl.queryInstanceStatus(hostName, 
            dataNode, self.envFile)
            if insType not in (ROLE_PRIMARY, ROLE_STANDBY, ROLE_NORMAL, ROLE_CASCADE):
                GaussLog.exitWithError(ErrorCode.GAUSS_357["GAUSS_35703"] % 
                (hostName, self.user, dataNode, dataNode))
        
        allHostIp = []
        allHostIp.append(self.context.localIp)
        versionDic = {}

        for hostip in standbyHosts:
            allHostIp.append(hostip)
        sshTool= SshTool(allHostIp)
        #get version in the nodes 
        getversioncmd = "gaussdb --version"
        resultMap, outputCollect = sshTool.getSshStatusOutput(getversioncmd,
                                                               [], envfile)
        self.cleanSshToolFile(sshTool)
        versionLines = outputCollect.splitlines()
        for i in range(int(len(versionLines)/2)):
            ipPattern = re.compile("\[.*\] (\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}):")
            ipKey = ipPattern.findall(versionLines[2 * i])[0]
            versionPattern = re.compile("gaussdb \((.*)\) .*")
            version = versionPattern.findall(versionLines[2 * i + 1])[0]
            versionDic[ipKey] = version
        for hostip in versionDic:
            if hostip == self.context.localIp:
               versionCompare = ""
               versionCompare = versionDic[hostip]
            else:
                if versionDic[hostip] == versionCompare:
                    continue
                else:
                    GaussLog.exitWithError(ErrorCode.GAUSS_357["GAUSS_35705"] \
                       %(hostip, versionDic[hostip]))
        self.logger.log("Successfully checked the database with locale mode.")

    def preInstall(self):
        """
        preinstall on new hosts.
        """
        self.logger.log("Start to preinstall database on the new \
standby nodes.")
        self.sendSoftToHosts()
        self.generateAndSendXmlFile()
        self.preInstallOnHosts()
        self.logger.log("Successfully preinstall database on the new \
standby nodes.")


    def clearTmpFile(self):
        """
        clear temporary file after expansion success
        """
        self.logger.debug("start to delete temporary file %s" % self.tempFileDir)
        clearCmd = "if [ -d '%s' ];then rm -rf %s;fi" % \
            (self.tempFileDir, self.tempFileDir)
        hostNames = self.context.nodeNameList
        try:
            sshTool = SshTool(hostNames)
            result, output = sshTool.getSshStatusOutput(clearCmd, 
            hostNames, self.envFile)
            self.logger.debug(output)
            self.cleanSshToolFile(sshTool)
        except Exception as e:
            self.logger.debug(str(e))
            self.cleanSshToolFile(sshTool)
        

    def cleanSshToolFile(self, sshTool):
        """
        """
        try:
            sshTool.clenSshResultFiles()
        except Exception as e:
            self.logger.debug(str(e))

    
    def checkNodesDetail(self):
        """
        """
        self.checkUserAndGroupExists()
        self.checkXmlFileAccessToUser()
        self.checkClusterStatus()
        self.validNodeInStandbyList()

    def checkClusterStatus(self):
        """
        Check whether the cluster status is normal before expand.
        """
        self.logger.debug("Start to check cluster status.\n")

        curHostName = socket.gethostname()
        command = "su - %s -c 'source %s;gs_om -t status --detail'" % \
            (self.user, self.envFile)
        sshTool = SshTool([curHostName])
        resultMap, outputCollect = sshTool.getSshStatusOutput(command, 
        [curHostName], self.envFile)
        if outputCollect.find("Primary Normal") == -1:
            GaussLog.exitWithError("Unable to query current cluster status. " + \
                "Please import environment variables or " +\
                "check whether the cluster status is normal.")
        
        self.logger.debug("The primary database is normal.\n")

    def validNodeInStandbyList(self):
        """
        check if the node has been installed in the cluster.
        """
        self.logger.debug("Start to check if the nodes in standby list\n")

        curHostName = socket.gethostname()
        command = "su - %s -c 'source %s;gs_om -t status --detail'" % \
            (self.user, self.envFile)
        sshTool = SshTool([curHostName])
        resultMap, outputCollect = sshTool.getSshStatusOutput(command, 
        [curHostName], self.envFile)
        self.logger.debug(outputCollect)

        newHosts = self.context.newHostList
        standbyHosts = []
        existHosts = []
        while len(newHosts) > 0:
            hostIp = newHosts.pop()
            nodeName = self.context.backIpNameMap[hostIp]
            nodeInfo = self.context.clusterInfoDict[nodeName]
            dataNode = nodeInfo["dataNode"]
            exist_reg = r"(.*)%s[\s]*%s(.*)" % (nodeName, hostIp)
            if not re.search(exist_reg, outputCollect):
                standbyHosts.append(hostIp)
            else:
                existHosts.append(hostIp)
        self.context.newHostList = standbyHosts
        if len(existHosts) > 0:
            self.logger.log("The nodes [%s] are already in the cluster. Skip expand these nodes." \
                % ",".join(existHosts))
        self.cleanSshToolFile(sshTool)
        if len(standbyHosts) == 0:
            self.logger.log("There is no node can be expanded.")
            sys.exit(0)

    def checkXmlFileAccessToUser(self):
        """
        Check if the xml config file has readable access to user.
        """
        userInfo = pwd.getpwnam(self.user)
        uid = userInfo.pw_uid
        gid = userInfo.pw_gid

        xmlFile = self.context.xmlFile
        fstat = os.stat(xmlFile)
        mode = fstat[stat.ST_MODE]
        if (fstat[stat.ST_UID] == uid and (mode & stat.S_IRUSR > 0)) or \
           (fstat[stat.ST_GID] == gid and (mode & stat.S_IRGRP > 0)):
            pass
        else:
            self.logger.debug("User %s has no access right for file %s" \
                 % (self.user, xmlFile))
            os.chown(xmlFile, uid, gid)
            os.chmod(xmlFile, stat.S_IRUSR)

    def checkUserAndGroupExists(self):
        """
        check system user and group exists and be same 
        on primary and standby nodes
        """
        inputUser = self.user
        inputGroup = self.group
        
        user_group_id = ""
        isUserExits = False
        localHost = socket.gethostname()
        for user in pwd.getpwall():
            if user.pw_name == self.user:
                user_group_id = user.pw_gid
                isUserExits = True
                break
        if not isUserExits:
            GaussLog.exitWithError(ErrorCode.GAUSS_357["GAUSS_35704"] \
                % ("User", self.user, localHost))

        isGroupExits = False
        group_id = ""
        for group in grp.getgrall():
            if group.gr_name == self.group:
                group_id = group.gr_gid
                isGroupExits = True
        if not isGroupExits:
            GaussLog.exitWithError(ErrorCode.GAUSS_357["GAUSS_35704"] \
                % ("Group", self.group, localHost))
        if user_group_id != group_id:
            GaussLog.exitWithError("User [%s] is not in the group [%s]."\
                 % (self.user, self.group))
        
        hostNames = self.context.newHostList
        envfile = self.envFile
        sshTool = SshTool(hostNames)

        #get username in the other standy nodes 
        getUserNameCmd = "cat /etc/passwd | grep -w %s" % inputUser
        resultMap, outputCollect = sshTool.getSshStatusOutput(getUserNameCmd, 
        [], envfile)
        
        for hostKey in resultMap:
            if resultMap[hostKey] == STATUS_FAIL:
                self.cleanSshToolFile(sshTool)
                GaussLog.exitWithError(ErrorCode.GAUSS_357["GAUSS_35704"] \
                       % ("User", self.user, hostKey))
        
        #get groupname in the other standy nodes   
        getGroupNameCmd = "cat /etc/group | grep -w %s" % inputGroup
        resultMap, outputCollect = sshTool.getSshStatusOutput(getGroupNameCmd, 
        [], envfile)
        for hostKey in resultMap:
            if resultMap[hostKey] == STATUS_FAIL:
                self.cleanSshToolFile(sshTool)
                GaussLog.exitWithError(ErrorCode.GAUSS_357["GAUSS_35704"] \
                       % ("Group", self.group, hostKey))
        self.cleanSshToolFile(sshTool)

    
    def installAndExpansion(self):
        """
        install database and expansion standby node with db om user
        """
        pvalue = Value('i', 0)
        proc = Process(target=self.installProcess, args=(pvalue,)) 
        proc.start()
        proc.join()
        if not pvalue.value:
            sys.exit(1)
        else:
            proc.terminate()

    def installProcess(self, pvalue):
        # change to db manager user. the below steps run with db manager user.
        self.changeUser()

        if not self.context.standbyLocalMode:
            self.logger.log("\nStart to install database on the new \
standby nodes.")
            self.installDatabaseOnHosts()
        else:
            self.logger.log("\nStandby nodes is installed with locale mode.")
            self.checkLocalModeOnStandbyHosts()

        self.logger.log("\nDatabase on standby nodes installed finished.")
        self.logger.log("\nStart to establish the primary-standby relationship.") 
        self.buildStandbyRelation()
        # process success
        pvalue.value = 1

    def rollback(self):
        """
        rollback all hosts' replconninfo about failed hosts 
        """
        existingHosts = self.existingHosts
        failedHosts = []
        for host in self.expansionSuccess.keys():
            if self.expansionSuccess[host]:
                existingHosts.append(host)
            else:
                failedHosts.append(host)
        clusterInfoDict = self.context.clusterInfoDict
        primaryHostName = self.getPrimaryHostName()
        for failedHost in failedHosts:
            self.logger.debug("start to rollback replconninfo about %s" % failedHost)
            for host in existingHosts:
                hostName = self.context.backIpNameMap[host]
                dataNode = clusterInfoDict[hostName]["dataNode"]
                confFile = os.path.join(dataNode, "postgresql.conf")
                rollbackReplconninfoCmd = "sed -i '/remotehost=%s/s/^/#&/' %s" \
                    % (failedHost, confFile)
                self.logger.debug(rollbackReplconninfoCmd)
                sshTool = SshTool(host)
                (statusMap, output) = sshTool.getSshStatusOutput(rollbackReplconninfoCmd, [host])
                if hostName == primaryHostName:
                    pg_hbaFile = os.path.join(dataNode, "pg_hba.conf")
                    rollbackPg_hbaCmd = "sed -i '/%s/s/^/#&/' %s" \
                        % (failedHost, pg_hbaFile)
                    self.logger.debug(rollbackPg_hbaCmd)
                    (statusMap, output) = sshTool.getSshStatusOutput(rollbackPg_hbaCmd, [host])
                reloadGUCCommand = "source %s ; gs_ctl reload -D %s " % \
                    (self.envFile, dataNode)
                self.logger.debug(reloadGUCCommand)
                resultMap, outputCollect = sshTool.getSshStatusOutput(
                    reloadGUCCommand, [host], self.envFile)
                self.logger.debug(outputCollect)
                self.cleanSshToolFile(sshTool)

    def run(self):
        """
        start expansion
        """
        self.checkNodesDetail()
        # preinstall on standby nodes with root user.
        if not self.context.standbyLocalMode:
            self.preInstall()

        self.installAndExpansion()
        self.logger.log("Expansion Finish.")


class GsCtlCommon:

    def __init__(self, expansion):
        """
        """
        self.logger = expansion.logger
        self.user = expansion.user
    
    def queryInstanceStatus(self, host, datanode, env):
        """
        """
        command = "source %s ; gs_ctl query -D %s" % (env, datanode)
        sshTool = SshTool([datanode])
        resultMap, outputCollect = sshTool.getSshStatusOutput(command, 
        [host], env)
        self.logger.debug(outputCollect)
        localRole = re.findall(r"local_role.*: (.*?)\n", outputCollect)
        db_state = re.findall(r"db_state.*: (.*?)\n", outputCollect)

        insType = ""

        if(len(localRole)) == 0:
            insType = ""
        else:
            insType = localRole[0]
        
        dbStatus = ""
        if(len(db_state)) == 0:
            dbStatus = ""
        else:
            dbStatus = db_state[0]
        self.cleanSshToolTmpFile(sshTool)
        return insType.strip().lower(), dbStatus.strip().lower()

    def stopInstance(self, host, datanode, env):
        """
        """
        command = "source %s ; gs_ctl stop -D %s" % (env, datanode)
        sshTool = SshTool([host])
        resultMap, outputCollect = sshTool.getSshStatusOutput(command, 
        [host], env)
        self.logger.debug(host)
        self.logger.debug(outputCollect)
        self.cleanSshToolTmpFile(sshTool)
    
    def startInstanceWithMode(self, host, datanode, mode, env):
        """
        """
        command = "source %s ; gs_ctl start -D %s -M %s" % (env, datanode, mode)
        self.logger.debug(command)
        sshTool = SshTool([host])
        resultMap, outputCollect = sshTool.getSshStatusOutput(command, 
        [host], env)
        self.logger.debug(host)
        self.logger.debug(outputCollect)
        self.cleanSshToolTmpFile(sshTool)

    def buildInstance(self, host, datanode, mode, env):
        command = "source %s ; gs_ctl build -D %s -M %s" % (env, datanode, mode)
        self.logger.debug(command)
        sshTool = SshTool([host])
        resultMap, outputCollect = sshTool.getSshStatusOutput(command, 
        [host], env)
        self.logger.debug(host)
        self.logger.debug(outputCollect)
        self.cleanSshToolTmpFile(sshTool)

    def startOmCluster(self, host, env):
        """
        om tool start cluster
        """
        command = "source %s ; gs_om -t start" % env
        self.logger.debug(command)
        sshTool = SshTool([host])
        resultMap, outputCollect = sshTool.getSshStatusOutput(command, 
        [host], env)
        self.logger.debug(host)
        self.logger.debug(outputCollect)
        self.cleanSshToolTmpFile(sshTool)
    
    def queryOmCluster(self, host, env):
        """
        query om cluster detail with command:
        gs_om -t status --detail
        """
        command = "source %s ; gs_om -t status --detail" % env
        sshTool = SshTool([host])
        resultMap, outputCollect = sshTool.getSshStatusOutput(command, 
        [host], env)
        self.logger.debug(host)
        self.logger.debug(outputCollect)
        if resultMap[host] == STATUS_FAIL:
            GaussLog.exitWithError("Query cluster failed. Please check " \
                "the cluster status or " \
                "source the environmental variables of user [%s]." % self.user)
        self.cleanSshToolTmpFile(sshTool)
        return outputCollect

    def cleanSshToolTmpFile(self, sshTool):
        """
        """
        try:
            sshTool.clenSshResultFiles()
        except Exception as e:
            self.logger.debug(str(e))



