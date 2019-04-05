# This file takes care of creating the log files with information
# Every X amount of time it goes to the database and reports

import multiprocessing
import sys
from datetime import datetime
from datetime import timedelta
import os
import threading
import time
from slips.core.database import __database__
import configparser
import pprint
import json

def timing(f):
    """ Function to measure the time another function takes. It should be used as decorator: @timing"""
    def wrap(*args):
        time1 = time.time()
        ret = f(*args)
        time2 = time.time()
        print('function took {:.3f} ms'.format((time2-time1)*1000.0))
        return ret
    return wrap

# Logs output Process
class LogsProcess(multiprocessing.Process):
    """ A class to output data in logs files """
    def __init__(self, inputqueue, outputqueue, verbose, debug, config):
        multiprocessing.Process.__init__(self)
        self.verbose = verbose
        self.debug = debug
        self.config = config
        # From the config, read the timeout to read logs. Now defaults to 5 seconds
        self.inputqueue = inputqueue
        self.outputqueue = outputqueue
        # Read the configuration
        self.read_configuration()
        self.fieldseparator = __database__.getFieldSeparator()
        # For some weird reason the database loses its outputqueue and we have to re set it here.......
        __database__.setOutputQueue(self.outputqueue)

    def read_configuration(self):
        """ Read the configuration file for what we need """
        # Get the time of log report
        try:
            self.report_time = int(self.config.get('parameters', 'log_report_time'))
        except (configparser.NoOptionError, configparser.NoSectionError, NameError):
            # There is a conf, but there is no option, or no section or no configuration file specified
            self.report_time = 5
        self.outputqueue.put('01|logs|Logs Process configured to report every: {} seconds'.format(self.report_time))

    def run(self):
        try:
            # Create our main output folder. The current datetime with microseconds
            # TODO. Do not create the folder if there is no data? (not sure how to)
            self.mainfoldername = datetime.now().strftime('%Y-%m-%d--%H:%M:%S')
            if not os.path.exists(self.mainfoldername):
                    os.makedirs(self.mainfoldername)
            # go into this folder
            os.chdir(self.mainfoldername)

            # Process the data with different strategies
            # Strategy 1: Every X amount of time
            # Create a timer to process the data every X seconds
            timer = TimerThread(self.report_time, self.process_global_data)
            timer.start()

            while True:
                line = self.inputqueue.get()
                if 'stop' != line:
                    # we are not processing input from the queue yet
                    # without this line the complete output thread does not work!!
                    # WTF???????
                    print(line)
                    pass
                else:
                    # Here we should still print the lines coming in the input for a while after receiving a 'stop'. We don't know how to do it.
                    self.outputqueue.put('stop')
                    return True
            # Stop the timer
            timer.shutdown()

        except KeyboardInterrupt:
            # Stop the timer
            timer.shutdown()
            return True
        except Exception as inst:
            # Stop the timer
            timer.shutdown()
            self.outputqueue.put('01|logs|\t[Logs] Error with LogsProcess')
            self.outputqueue.put('01|logs|\t[Logs] {}'.format(type(inst)))
            self.outputqueue.put('01|logs|\t[Logs] {}'.format(inst))
            sys.exit(1)

    def createProfileFolder(self, profileid):
        """
        Receive a profile id, create a folder if its not there. Create the log files.
        """
        # Ask the field separator to the db
        profilefolder = profileid.split(self.fieldseparator)[1]
        if not os.path.exists(profilefolder):
            os.makedirs(profilefolder)
            # If we create the folder, add once there the profileid. We have to do this here if we want to do it once.
            self.addDataToFile(profilefolder + '/' + 'ProfileData.txt', 'Profileid : ' + profileid)
        return profilefolder

    def addDataToFile(self, filename, data, file_mode='w+', data_type='txt', data_mode='text'):
        """
        Receive data and append it in the general log of this profile
        If the filename was not opened yet, then open it, write the data and return the file object.
        Do not close the file
        In data_mode = 'text', we add a \n at the end
        In data_mode = 'raw', we do not add a \n at the end
        """
        if data_type == 'json':
            # Implement some fancy print from json data
            data = data
        if data_mode == 'text':
            data = data + '\n'
        try:
            filename.write(data)
            return filename
        except (NameError, AttributeError) as e:
            # The file was not opened
            fileobj = open(filename, file_mode)
            fileobj.write(data)
            # For some reason the files are closed and flushed correclty.
            return fileobj
        except KeyboardInterrupt:
            return True

    def process_global_data(self):
        """ 
        This is the main function called by the timer process
        Read the global data and output it on logs 
        """
        try:
            #1. Get the list of profiles so far
            #temp_profs = __database__.getProfiles()
            #if not temp_profs:
            #    return True
            #profiles = list(temp_profs)

            # How many profiles we have?
            profilesLen = str(__database__.getProfilesLen())
            # Get the list of all the modifed TW for all the profiles
            TWforProfile = __database__.getModifiedTWLogs()
            amount_of_modified = len(TWforProfile)
            self.outputqueue.put('20|logs|[Logs] Number of Profiles in DB: {}. Modified TWs: {}. ({})'.format(profilesLen, amount_of_modified , datetime.now().strftime('%Y-%m-%d--%H:%M:%S')))
            for profileTW in TWforProfile:
                # Get the profileid and twid
                profileid = profileTW.split(self.fieldseparator)[0] + self.fieldseparator + profileTW.split(self.fieldseparator)[1]
                twid = profileTW.split(self.fieldseparator)[2]
                # Get the time of this TW. For the file name
                twtime = __database__.getTimeTW(profileid, twid)
                twtime = time.strftime('%Y-%m-%dT%H:%M:%S', time.localtime(twtime))
                self.outputqueue.put('02|logs|\t[Logs] Storing Profile: {}. TW {}. Time: {}'.format(profileid, twid, twtime))
                #self.outputqueue.put('30|logs|\t[Logs] Profile: {} has {} timewindows'.format(profileid, twLen))
                # Create the folder for this profile if it doesn't exist
                profilefolder = self.createProfileFolder(profileid)
                twlog = twtime + '.' + twid
                # Add data into profile log file
                # First Erase its file and save the data again
                self.addDataToFile(profilefolder + '/' + twlog, '', file_mode='w+', data_mode='raw')

                # Save in the log file all parts of the profile

                # 0. Write the profileID for people getting know what they see in the file.
                self.addDataToFile(profilefolder + '/' + twlog, 'ProfileID: {}\n'.format(profileid), file_mode='a+', data_type='text')

                # 1. Detections to block. The getBlockingRequest function return {True, False}
                blocking = __database__.getBlockingRequest(profileid, twid)
                if blocking:
                    text_data = 'Was requested to block in this time window: ' + str(blocking)
                    self.addDataToFile(profilefolder + '/' + twlog, text_data, file_mode='a+', data_type='json')
                    self.outputqueue.put('03|logs|\t\t[Logs] Blocking Request: ' + str(blocking))

                # 2. Info about the evidence so far for this TW.
                evidence = __database__.getEvidenceForTW(profileid, twid)
                if evidence:
                    evidence = json.loads(evidence)
                    self.addDataToFile(profilefolder + '/' + twlog, 'Evidence of detections in this TW:', file_mode='a+', data_type='text')
                    self.outputqueue.put('03|logs|\t\t[Logs] Evidence of detections in this TW:')
                    for data in evidence:
                        self.addDataToFile(profilefolder + '/' + twlog, '\tEvidence: {}'.format(data[0]), file_mode='a+', data_type='text')
                        self.outputqueue.put('03|logs|\t\t\t Evidence: {}'.format(data[0]))

                # 3. DstIPs
                dstips = __database__.getDstIPsfromProfileTW(profileid, twid)
                if dstips:
                    # Add dstips to log file
                    self.addDataToFile(profilefolder + '/' + twlog, 'DstIP:', file_mode='a+', data_type='text')
                    self.outputqueue.put('03|logs|\t\t[Logs] DstIP:')
                    data = json.loads(dstips)
                    # Better printing of data
                    for key in data:
                        self.addDataToFile(profilefolder + '/' + twlog, '\t{} ({} times)'.format(key, data[key]), file_mode='a+', data_type='text')
                        self.outputqueue.put('03|logs|\t\t\t[Logs] {} ({} times)'.format(key, data[key]))

                # 4. SrcIPs
                srcips = __database__.getSrcIPsfromProfileTW(profileid, twid)
                if srcips:
                    # Add srcips
                    self.addDataToFile(profilefolder + '/' + twlog, 'SrcIP:', file_mode='a+', data_type='text')
                    self.outputqueue.put('03|logs|\t\t[Logs] SrcIP:')
                    data = json.loads(srcips)
                    for key in data:
                        self.addDataToFile(profilefolder + '/' + twlog, '\t{} ({} times)'.format(key, data[key]), file_mode='a+', data_type='text')
                        self.outputqueue.put('03|logs|\t\t\t[Logs] {} ({} times)'.format(key, data[key]))

                # 5. OutTuples
                out_tuples = __database__.getOutTuplesfromProfileTW(profileid, twid)
                if out_tuples:
                    # Add tuples
                    self.addDataToFile(profilefolder + '/' + twlog, 'OutTuples:', file_mode='a+', data_type='text')
                    self.outputqueue.put('03|logs|\t\t[Logs] OutTuples:')
                    data = json.loads(out_tuples)
                    for key in data:
                        self.addDataToFile(profilefolder + '/' + twlog, '\t{} ({})'.format(key, data[key]), file_mode='a+', data_type='text')
                        self.outputqueue.put('03|logs|\t\t\t[Logs] {} ({})'.format(key, data[key]))

                # 6. InTuples
                out_tuples = __database__.getInTuplesfromProfileTW(profileid, twid)
                if out_tuples:
                    # Add tuples
                    self.addDataToFile(profilefolder + '/' + twlog, 'InTuples:', file_mode='a+', data_type='text')
                    self.outputqueue.put('03|logs|\t\t[Logs] InTuples:')
                    data = json.loads(out_tuples)
                    for key in data:
                        self.addDataToFile(profilefolder + '/' + twlog, '\t{} ({})'.format(key, data[key]), file_mode='a+', data_type='text')
                        self.outputqueue.put('03|logs|\t\t\t[Logs] {} ({})'.format(key, data[key]))

                """
                Dst ports and Src ports
                """
                for client_or_server, sentence in zip(['Client', 'Server'], ['As a client, Dst', 'As a server, Src']):
                    # 1. Info of dstport as client, tcp, established
                    dstportdata = __database__.getSrcDstPortTCPEstablishedFromProfileTW(profileid, twid, client_or_server)
                    if dstportdata:
                        text_data = sentence + ' Ports we connected with TCP Established flows:'
                        self.addDataToFile(profilefolder + '/' + twlog, text_data, file_mode='a+', data_type='text')
                        for port in dstportdata:
                            text_data = '\tPort {}. Total Flows: {}. Total Pkts: {}. TotalBytes: {}.'.format(port, dstportdata[port]['totalflows'], dstportdata[port]['totalpkt'], dstportdata[port]['totalbytes'])
                            self.addDataToFile(profilefolder + '/' + twlog, text_data, file_mode='a+', data_type='text')
                            self.outputqueue.put('03|logs|\t\t\t[Logs]: ' + text_data)

                    # 2. Info of dstport as client, udp, established
                    dstportdata = __database__.getSrcDstPortUDPEstablishedFromProfileTW(profileid, twid, client_or_server)
                    if dstportdata:
                        text_data = sentence + ' Ports we connected with UDP Established flows:'
                        self.addDataToFile(profilefolder + '/' + twlog, text_data, file_mode='a+', data_type='text')
                        for port in dstportdata:
                            text_data = '\tPort {}. Total Flows: {}. Total Pkts: {}. TotalBytes: {}.'.format(port, dstportdata[port]['totalflows'], dstportdata[port]['totalpkt'], dstportdata[port]['totalbytes'])
                            self.addDataToFile(profilefolder + '/' + twlog, text_data, file_mode='a+', data_type='text')
                            self.outputqueue.put('03|logs|\t\t\t[Logs]: {}'.format(text_data))

                    # 3. Info of dstport as client, tcp, notestablished
                    dstportdata = __database__.getSrcDstPortTCPNotEstablishedFromProfileTW(profileid, twid, client_or_server)
                    if dstportdata:
                        text_data = sentence + ' Ports we connected with TCP Not Established flows:'
                        self.addDataToFile(profilefolder + '/' + twlog, text_data, file_mode='a+', data_type='text')
                        for port in dstportdata:
                            text_data = '\tPort {}. Total Flows: {}. Total Pkts: {}. TotalBytes: {}.'.format(port, dstportdata[port]['totalflows'], dstportdata[port]['totalpkt'], dstportdata[port]['totalbytes'])
                            self.addDataToFile(profilefolder + '/' + twlog, text_data, file_mode='a+', data_type='text')
                            self.outputqueue.put('03|logs|\t\t\t[Logs]: {}'.format(text_data))

                    # 4. Info of dstport as client, udp, notestablished
                    dstportdata = __database__.getSrcDstPortUDPNotEstablishedFromProfileTW(profileid, twid, client_or_server)
                    if dstportdata:
                        text_data = sentence + ' Ports we connected with UDP Not Established flows:'
                        self.addDataToFile(profilefolder + '/' + twlog, text_data, file_mode='a+', data_type='text')
                        for port in dstportdata:
                            text_data = '\tPort {}. Total Flows: {}. Total Pkts: {}. TotalBytes: {}.'.format(port, dstportdata[port]['totalflows'], dstportdata[port]['totalpkt'], dstportdata[port]['totalbytes'])
                            self.addDataToFile(profilefolder + '/' + twlog, text_data, file_mode='a+', data_type='text')
                            self.outputqueue.put('03|logs|\t\t\t[Logs]: {}'.format(dstportdata))

                    # 5. Info of dstport as client, icmp, established
                    dstportdata = __database__.getSrcDstPortICMPEstablishedFromProfileTW(profileid, twid, client_or_server)
                    if dstportdata:
                        text_data = sentence + ' type we connected with ICMP Established flows:'
                        self.addDataToFile(profilefolder + '/' + twlog, text_data, file_mode='a+', data_type='text')
                        for port in dstportdata:
                            text_data = '\tType {}. Total Flows: {}. Total Pkts: {}. TotalBytes: {}.'.format(port, dstportdata[port]['totalflows'], dstportdata[port]['totalpkt'], dstportdata[port]['totalbytes'])
                            self.addDataToFile(profilefolder + '/' + twlog, text_data, file_mode='a+', data_type='text')
                            self.outputqueue.put('03|logs|\t\t\t[Logs]: {}'.format(text_data))

                    # 6. Info of dstport as client, icmp, notestablished
                    dstportdata = __database__.getSrcDstPortICMPNotEstablishedFromProfileTW(profileid, twid, client_or_server)
                    if dstportdata:
                        text_data = sentence + ' type we connected with ICMP Not Established flows:'
                        self.addDataToFile(profilefolder + '/' + twlog, text_data, file_mode='a+', data_type='text')
                        for port in dstportdata:
                            text_data = '\tType {}. Total Flows: {}. Total Pkts: {}. TotalBytes: {}.'.format(port, dstportdata[port]['totalflows'], dstportdata[port]['totalpkt'], dstportdata[port]['totalbytes'])
                            self.addDataToFile(profilefolder + '/' + twlog, text_data, file_mode='a+', data_type='text')
                            self.outputqueue.put('03|logs|\t\t\t[Logs]: {}'.format(text_data))

                    # 7. Info of dstport as client, ipv6-icmp, established
                    dstportdata = __database__.getSrcDstPortIPV6ICMPEstablishedFromProfileTW(profileid, twid, client_or_server)
                    if dstportdata:
                        text_data = sentence + ' type we connected with IPv6ICMP Established flows:'
                        self.addDataToFile(profilefolder + '/' + twlog, text_data, file_mode='a+', data_type='text')
                        self.outputqueue.put('03|logs|\t\t[Logs]: {}'.format(text_data))
                        for port in dstportdata:
                            text_data = '\tType {}. Total Flows: {}. Total Pkts: {}. TotalBytes: {}.'.format(port, dstportdata[port]['totalflows'], dstportdata[port]['totalpkt'], dstportdata[port]['totalbytes'])
                            self.addDataToFile(profilefolder + '/' + twlog, text_data, file_mode='a+', data_type='text')
                            self.outputqueue.put('03|logs|\t\t\t[Logs]: {}'.format(text_data))

                    # 8. Info of dstport as client, ipv6icmp, notestablished
                    dstportdata = __database__.getSrcDstPortIPV6ICMPNotEstablishedFromProfileTW(profileid, twid, client_or_server)
                    if dstportdata:
                        text_data = sentence + ' type we connected with IPv6ICMP Not Established flows:'
                        self.addDataToFile(profilefolder + '/' + twlog, text_data, file_mode='a+', data_type='text')
                        self.outputqueue.put('03|logs|\t\t[Logs]: {}'.format(text_data))
                        for port in dstportdata:
                            text_data = '\tType {}. Total Flows: {}. Total Pkts: {}. TotalBytes: {}.'.format(port, dstportdata[port]['totalflows'], dstportdata[port]['totalpkt'], dstportdata[port]['totalbytes'])
                            self.addDataToFile(profilefolder + '/' + twlog, text_data, file_mode='a+', data_type='text')
                            self.outputqueue.put('03|logs|\t\t\t[Logs]: {}'.format(text_data))

                # Mark it as not modified anymore
                __database__.markProfileTWAsNotModifiedLogs(profileid, twid)


            # Create the file of the blocked profiles and TW
            TWforProfileBlocked = __database__.getBlockedTW()
            # Create the file of blocked data
            if TWforProfileBlocked:
                self.addDataToFile('Blocked.txt', str(TWforProfileBlocked) + '\n', file_mode='w+', data_type='json')
                self.outputqueue.put('03|logs|\t\t[Logs]: Blocked file updated: {}'.format(TWforProfileBlocked))

        except KeyboardInterrupt:
            return True
        except Exception as inst:
            self.outputqueue.put('01|logs|\t[Logs] Error in process_global_data in LogsProcess')
            self.outputqueue.put('01|logs|\t[Logs] {}'.format(type(inst)))
            self.outputqueue.put('01|logs|\t[Logs] {}'.format(inst))
            sys.exit(1)


class TimerThread(threading.Thread):
    """Thread that executes a task every N seconds. Only to run the process_global_data."""
    
    def __init__(self, interval, function):
        threading.Thread.__init__(self)
        self._finished = threading.Event()
        self._interval = interval
        self.function = function 

    def shutdown(self):
        """Stop this thread"""
        self._finished.set()
    
    def run(self):
        try:
            while 1:
                if self._finished.isSet(): return
                self.task()
                
                # sleep for interval or until shutdown
                self._finished.wait(self._interval)
        except KeyboardInterrupt:
            return True
    
    def task(self):
        self.function()
