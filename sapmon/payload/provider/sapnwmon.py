# Python modules
import hashlib
import json
import logging
import re
import time
from datetime import datetime, timedelta, time

# Payload modules
from const import *
from helper.azure import *
from helper.context import *
from helper.tools import *
from provider.base import ProviderInstance, ProviderCheck
from typing import Dict, List

# SAP modules.
from pyrfc import Connection, ABAPApplicationError, ABAPRuntimeError, LogonError, CommunicationError

class SAPNWMonProviderInstance(ProviderInstance):
    def __init__(self,
                tracer: logging.Logger,
                ctx: Context,
                providerInstance: Dict[str, str],
                skipContent: bool = False,
                **kwargs):
        self.sapHostName = None
        self.sapSysNr = None
        self.sapClient = None
        self.sapUsername = None
        self.sapPassword = None

        retrySettings = {
         "retries": 3,
         "delayInSeconds": 1,
         "backoffMultiplier": 2
        }

        super().__init__(tracer,
                       ctx,
                       providerInstance,
                       retrySettings,
                       skipContent,
                       **kwargs)

    """
    parse provider properties and get host name, sysnr, client, username, password.
    """
    def parseProperties(self) -> bool:
        self.sapHostName = self.providerProperties.get("sapHostName", None)
        if not self.sapHostName:
            self.tracer.error("[%s] sapHostName cannot be empty" % self.fullName)
            return False
        
        self.sapSysNr = self.providerProperties.get("sapSysNr", None)
        if not self.sapSysNr:
            self.tracer.error("[%s] sapSysNr cannot be empty" % self.fullName)
            return False

        self.sapClient = self.providerProperties.get("sapClient", None)
        if not self.sapClient:
            self.tracer.error("[%s] sapClient cannot be empty" % self.fullName)
            return False

        self.sapUsername = self.providerProperties.get("sapUsername", None)
        if not self.sapUsername:
            self.tracer.error("[%s] sapUsername cannot be empty" % self.fullName)
            return False

        self.sapPassword = self.providerProperties.get("sapPassword", None)
        if not self.sapPassword:
            self.tracer.error("[%s] sapPassword cannot be empty" % self.fullName)
            return False

        return True

    def validate(self) -> bool:
        self.tracer.info("connecting to sap with host name (%s) to test required rfc calls.")

        # establish connection to SAP using provided credentials.
        # TODO: log times required for calls.
        try:
            with self._establish_connection_to_sap() as connection:
                if connection is None:
                    return False

                # test call RFCPING.
                connection.call('RFCPING')
                self.tracer.info("successfully called RFC ping.")

                # test call 
                currentTimestampResult = self._call_bdl_get_central_timestamp(connection)
                if currentTimestampResult is None:
                    return False
                currentDate, currentTime = self._process_bdl_get_central_timestamp_result(currentTimestampResult)
                self.tracer.info("successfully retrieved timestamp from BDL_GET_CENTRAL_TIMESTAMP")

                smon_result = self._call_sdf_get_smon_runs(connection, currentDate, currentDate)
                if smon_result is None:
                    return False
                guid = self._process_guid_using_smon_runs(smon_result)
                self.tracer.info("successfully retrieved GUID from /SDF/GET_SMON_RUNS.")

                # TODO: if smon analysis does not returns results raise error.
                # test if sdf/smon_analysis_run returns result for last minute.
                startTime = (datetime.combine(date(year=1, month=1, day=1), currentTime) - timedelta(minutes=1)).time()
                # for test
                currentDate = date(2020, 10, 22)
                startTime = time(0, 1)
                currentTime = time(23, 59)
                ##
                smon_analysis_result = self._call_sdf_smon_analysis_read(connection, guid, currentDate, startTime, currentTime)
                if smon_analysis_result is None:
                    return False
                processedResult = self._process_sdf_smon_analysis_read(smon_analysis_result)
                if len(processedResult) == 0:
                    self.tracer.error("RFC SDF/SMON_ANALYSIS_READ result did not return values. Check RFC setup.")
                    return False
        except Exception as e:
            self.tracer.error("Error occured while validating %s " % (e))
            return False

        return True

    # establish connection to sap.
    def _establish_connection_to_sap(self) -> Connection:
        try:
            connection = Connection(ashost=self.sapHostName, sysnr=self.sapSysNr, client=self.sapClient, user=self.sapUsername, passwd=self.sapPassword)
        except CommunicationError as e:
            self.tracer.error("Cannot establish connection with (%s) with hostname: %s " % (self.fullName, self.sapHostName))
            return None
        except LogonError as e:
            self.tracer.error("Incorrect credentials used to connect with hostname: %s username: %s" % (self.fullName, self.sapUsername))
            return None
        except Exception as e:
            self.tracer.error("Error occured while establishing connection (%s) " % (e))
            return None

        return connection

    def _call_bdl_get_central_timestamp(self, connection: Connection):
        timestampResult = None
        try:
            timestampResult = connection.call('BDL_GET_CENTRAL_TIMESTAMP')
        except CommunicationError as e:
            self.tracer.error("Cannot establish connection with (%s) with hostname: %s " % (self.fullName, self.sapHostName))
            return None
        except Exception as e:
            self.tracer.error("Error occured while calling rfc BDL_GET_CENTRAL_TIMESTAMP (%s) " % (e))
            return None

        return timestampResult

    def _process_bdl_get_central_timestamp_result(self, result: Dict[str, str]):
        if result is None:
            raise ValueError("Invalid result received from BDL_GET_CENTRAL_TIMESTAMP")

        currentDate: date = None
        currentTime: time = None

        if 'TAG' in result:
            currentDate = datetime.strptime(result['TAG'], '%Y%m%d').date()
        else:
            raise ValueError("RFC BDL_GET_CENTRAL_TIMESTAMP result does not have TAG value.")

        if 'UHRZEIT' in result:
            currentTime = datetime.strptime(result['UHRZEIT'], '%H%M%S').time()
        else:
            raise ValueError("RFC BDL_GET_CENTRAL_TIMESTAMP result does not have TAG value.")

        return currentDate, currentTime

    def _call_sdf_get_smon_runs(self, connection: Connection, fromDate: date=None, toDate: date=None):
        if fromDate is None:
            fromDate = datetime(1971, 5, 20).date()

        if toDate is None:
            toDate = datetime(2999, 12, 31).date()
        try:
            smon_result = connection.call('/SDF/SMON_GET_SMON_RUNS', FROM_DATE=fromDate, TO_DATE=toDate)
        except CommunicationError as e:
            self.tracer.error("Cannot establish connection with (%s) with hostname: %s " % (self.fullName, self.sapHostName))
            return None
        except Exception as e:
            self.tracer.error("Error occured while calling /SDF/SMON_GET_SMON_RUNS (%s) " % (e))
            return None

        return smon_result

    # parse result from /SDF/SMON_GET_SMON_RUNS and return GUID.
    def _process_guid_using_smon_runs(self, result):
        if 'SMON_RUNS' in result:
            if 'GUID' in result['SMON_RUNS'][0]:
                return result['SMON_RUNS'][0]['GUID']
            else:
                raise ValueError("GUID value does not exist in /SDF/SMON_GET_SMON_RUNS return result.")
        else:
            raise ValueError("SMON_RUNS value does not exist in /SDF/SMON_GET_SMON_RUNS return result.")

    # call RFC SDF/SMON_ANALYSIS_RUN and return the result.
    def _call_sdf_smon_analysis_read(self, connection: Connection, guid: str, currentDate: date, startTime: time, endTime: time):
        result = None
        try:
            result = connection.call('/SDF/SMON_ANALYSIS_READ', GUID=guid, DATUM=currentDate, START_TIME=startTime, END_TIME=endTime)
        except CommunicationError as e:
            self.tracer.error("Cannot establish connection with (%s) with hostname: %s " % (self.fullName, self.sapHostName))
            return None
        except Exception as e:
            self.tracer.error("Error occured while calling RFC /SDF/SMON_ANALYSIS_READ (%s) " % (e))
            return None

        return result

    # return header information from sdf/smon_analysis_read
    def _process_sdf_smon_analysis_read(self, result, filterList: List[str]=None):
        if result is None:
            raise ValueError("Invalid result received from SDF/SMON_ANALYSIS_READ")

        processedResult = None
        if 'HEADER' in result:
            # create new dictionary with only values from filterList if filter dictionary exists.
            processedResult = result['HEADER']
            if filterList:
                processedResult = { columnName: processedResult[columnName] for columnName in filterList }
        else:
            raise ValueError("SDF/SMON_ANALYSIS_READ result does not contain HEADER key.")

        return processedResult

###########################
# implement sapnwmon check.
class SAPNWMonProviderCheck(ProviderCheck):

    def __init__(self,
    provider: ProviderInstance,
    **kwargs
    ):
        super.__init__(provider, **kwargs)

    def generateJsonString(self) -> str:
        return None

    def updateState(self):
        return

