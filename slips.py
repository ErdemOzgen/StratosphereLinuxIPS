#!/usr/bin/env python3
# Stratosphere Linux IPS. A machine-learning Intrusion Detection System
# Copyright (C) 2021 Sebastian Garcia

# This program is free software; you can redistribute it and/or
# modify it under the terms of the GNU General Public License
# as published by the Free Software Foundation; either version 2
# of the License, or (at your option) any later version.

# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.

# You should have received a copy of the GNU General Public License
# along with this program; if not, write to the Free Software
# Foundation, Inc., 51 Franklin Street, Fifth Floor, Boston, MA  02110-1301, USA.
# Contact: eldraco@gmail.com, sebastian.garcia@agents.fel.cvut.cz, stratosphere@aic.fel.cvut.cz

from modules.update_manager.update_file_manager import UpdateFileManager
from slips_files.common.abstracts import Module
from slips_files.common.argparse import ArgumentParser
from slips_files.common.slips_utils import utils
from slips_files.core.database import __database__
import configparser
import signal
import sys
import redis
import os
import time
import shutil
from datetime import datetime
import socket
import warnings
import pty
import json
import pkgutil
import inspect
import modules
import importlib
import errno
import subprocess
import re
import random
from collections import OrderedDict
from distutils.dir_util import copy_tree
import asyncio
from signal import SIGTERM

version = '0.9.0'

# Ignore warnings on CPU from tensorflow
os.environ['TF_CPP_MIN_LOG_LEVEL'] = '2'
# Ignore warnings in general
warnings.filterwarnings('ignore')
#---------------------


class Daemon():
    description = 'This module runs when slips is in daemonized mode'

    def __init__(self, slips):
        # to use read_configurations defined in Main
        self.slips = slips
        self.read_configuration()
        # Get the pid from pidfile
        try:
            with open(self.pidfile, 'r') as pidfile:
                self.pid = int(pidfile.read().strip())
        except IOError:
            self.pid = None

    def print(self, text):
        """ Prints output to logsfile specified in slips.conf"""
        with open(self.logsfile, 'a') as f:
            f.write(f'{text}\n')

    def setup_std_streams(self):
        """ Create standard steam files and dirs and clear them """

        std_streams = [self.stderr, self.stdout, self.logsfile]
        for file in std_streams:
            # we don't want to clear the logfile when we stop the daemon using -S
            if '-S' in sys.argv and file == self.stdout:
                continue
            # create the file if it doesn't exist or clear it if it exists
            try:
                open(file, 'w').close()
            except (FileNotFoundError,NotADirectoryError):
                os.mkdir(os.path.dirname(file))
                open(file, 'w').close()

    def read_configuration(self):
        """ Read the configuration file to get stdout, stderr, logsfile path."""
        self.config = self.slips.read_conf_file()

        try:
            # output dir to store running.log and error.log
            self.output_dir = self.config.get('modes', 'output_dir')
            if not self.output_dir.endswith('/'):
                self.output_dir = self.output_dir + '/'
        except (configparser.NoOptionError, configparser.NoSectionError, NameError):
            # There is a conf, but there is no option, or no section or no configuration file specified
            self.output_dir = '/var/log/slips/'

        try:
            # this file has info about the daemon, started, ended, pid , etc.. by default it's the same as stdout
            self.logsfile = self.config.get('modes', 'logsfile')
            self.logsfile = self.output_dir + self.logsfile
        except (configparser.NoOptionError, configparser.NoSectionError, NameError):
            # There is a conf, but there is no option, or no section or no configuration file specified
            self.logsfile = '/var/log/slips/running.log'

        try:
            self.stdout = self.config.get('modes', 'stdout')
            self.stdout = self.output_dir + self.stdout
        except (configparser.NoOptionError, configparser.NoSectionError, NameError):
            # There is a conf, but there is no option, or no section or no configuration file specified
            self.stdout = '/var/log/slips/running.log'

        try:
            self.stderr = self.config.get('modes', 'stderr')
            self.stderr = self.output_dir + self.stderr
        except (configparser.NoOptionError, configparser.NoSectionError, NameError):
            # There is a conf, but there is no option, or no section or no configuration file specified
            self.stderr = '/var/log/slips/errors.log'

        # this is a conf file used to store the pid of the daemon and is deleted when the daemon stops
        self.pidfile_dir = '/etc/slips/'
        self.pidfile = os.path.join(self.pidfile_dir, 'pidfile')

        # we don't use it anyway
        self.stdin = '/dev/null'

        # this is where alerts.log and alerts.json are stored, in interactive mode
        # they're stored in output/ dir in slips main dir
        # in daemonized mode they're stored in the same dir as running.log and error.log
        self.slips.alerts_default_path = self.output_dir

        self.setup_std_streams()
        # when stopping the daemon don't log this info again
        if '-S' not in sys.argv:
            self.print(f"Logsfile: {self.logsfile}\n"
                       f"pidfile: {self.pidfile}\n"
                       f"stdin : {self.stdin}\n"
                       f"stdout: {self.stdout}\n"
                       f"stderr: {self.stderr}\n")
            self.print("Done reading configuration and setting up files.\n")

    def terminate(self):
        """ Deletes the pidfile to mark the daemon as closed """

        self.print("Deleting pidfile...")

        if os.path.exists(self.pidfile):
            os.remove(self.pidfile)
            self.print("pidfile deleted.")
        else:
            self.print(f"Can't delete pidfile, {self.pidfile} doesn't exist.")
            # if an error occured it will be written in logsfile
            self.print("Either Daemon stopped normally or an error occurred.")
            self.print("pidfile needs to be deleted before running Slips again.")

    def daemonize(self):
        """
        Does the Unix double-fork to create a daemon
        """
        # double fork explaination
        # https://stackoverflow.com/questions/881388/what-is-the-reason-for-performing-a-double-fork-when-creating-a-daemon

        try:
            self.pid = os.fork()
            if self.pid > 0:
                # exit first parent
                sys.exit(0)
        except OSError as e:
            sys.stderr.write(f"Fork #1 failed: {e.errno} {e.strerror}\n")
            self.print(f"Fork #1 failed: {e.errno} {e.strerror}\n")
            sys.exit(1)

        # os.chdir("/")
        # dissociate the daemon from its controlling terminal.
        # calling setsid means that this child will be the session leader of the new session
        os.setsid()
        os.umask(0)

        # If you want to prevent a process from acquiring a tty, the process shouldn't be the session leader
        # fork again so that the second child is no longer a session leader
        try:
            self.pid = os.fork()
            if self.pid > 0:
                # exit from second parent (aka first child)
                sys.exit(0)
        except OSError as e:
            sys.stderr.write(f"Fork #2 failed: {e.errno} {e.strerror}\n")
            self.print(f"Fork #2 failed: {e.errno} {e.strerror}\n")
            sys.exit(1)

        # Now this code is run from the daemon
        # A daemon must close it's input and output file descriptors otherwise, it would still
        # be attached to the terminal it was started in.
        sys.stdout.flush()
        sys.stderr.flush()

        # redirect standard file descriptors
        with open(self.stdin, 'r') as stdin,\
            open(self.stdout, 'a+') as stdout,\
            open(self.stderr, 'a+') as stderr:
            os.dup2(stdin.fileno(), sys.stdin.fileno())
            os.dup2(stdout.fileno(), sys.stdout.fileno())
            os.dup2(stderr.fileno(), sys.stderr.fileno())

        # write the pid of the daemon to a file so we can check if it's already opened before re-opening
        if not os.path.exists(self.pidfile_dir):
            os.mkdir(self.pidfile_dir)

        self.pid = str(os.getpid())
        with open(self.pidfile, 'w+') as pidfile:
            pidfile.write(self.pid)

        # Register a function to be executed if sys.exit() is called or the main module’s execution completes
        # atexit.register(self.terminate)

    def start(self):
        """ Main function, Starts the daemon and starts slips normally."""
        self.print("Daemon starting...")
        # Check for a pidfile to see if the daemon is already running
        if self.pid:
            self.print(f"pidfile {self.pid} already exists. Daemon already running?")
            sys.exit(1)

        # Start the daemon
        self.daemonize()

        # any code run after daemonizing will be run inside the daemon
        self.print(f"Slips Daemon is running. [PID {self.pid}]\n")
        # tell Main class that we're running in daemonized mode
        self.slips.set_mode('daemonized', daemon=self)
        # start slips normally
        self.slips.start()

    def stop(self):
        """Stop the daemon"""
        if not self.pid:
            self.print(f"Trying to stop Slips daemon. PID {self.pid} doesn't exist. Daemon not running.")
            return

        # Try killing the daemon process
        try:
            # delete the pid file
            self.terminate()
            self.print(f"Daemon killed [PID {self.pid}]")
            while 1:
                os.kill(int(self.pid), SIGTERM)
                time.sleep(0.1)
        except OSError as e:
            e = str(e)
            if e.find("No such process") <= 0:
                # some error occured, print it
                self.print(e)

    def restart(self):
        """Restart the daemon"""
        self.print("Daemon restarting...")
        self.stop()
        self.pid = False
        self.start()

class Main():
    def __init__(self):
        # Set up the default path for alerts.log and alerts.json. In our case, it is output folder.
        self.alerts_default_path = 'output/'
        self.mode = 'interactive'
        self.used_redis_servers = 'used_redis_servers.txt'

    def read_configuration(self, config, section, name):
        """ Read the configuration file for what slips.py needs. Other processes also access the configuration """
        try:
            return config.get(section, name)
        except (configparser.NoOptionError, configparser.NoSectionError, NameError):
            # There is a conf, but there is no option, or no section or no configuration file specified
            return False


    def recognize_host_ip(self):
        """
        Recognize the IP address of the machine
        """
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.connect(("1.1.1.1", 80))
            ipaddr_check = s.getsockname()[0]
            s.close()
        except (socket.error):
            # not connected to the internet
            return None
        return ipaddr_check


    def create_folder_for_logs(self):
        """
        Create a folder for logs if logs are enabled
        """
        logs_folder = datetime.now().strftime('%Y-%m-%d--%H-%M-%S')
        try:
            os.makedirs(logs_folder)
        except OSError as e:
            if e.errno != errno.EEXIST:
                # doesn't exist and can't create
                return False
        return logs_folder

    def check_redis_database(self, redis_host='localhost', redis_port=6379) -> bool:
        """
        Check if we have redis-server running (this is the cache db it should always be running)
        """
        try:
            r = redis.StrictRedis(host=redis_host,
                                  port=redis_port,
                                  db=0,
                                  charset="utf-8",
                                  decode_responses=True)
            r.ping()
        except Exception as ex:
            print('[DB] Error: Is redis cache database running? '
                  'You can run it as: "redis-server --daemonize yes"')
            return False

        return True

    def generate_random_redis_port(self):
        """ Keeps trying to connect to random generated ports until we're connected.
            returns the used port
        """
        try:
            while True:
                # generate a random unused port
                port = random.randint(32768, 65535)
                # check if 1. we can connect
                can_connect = __database__.connect_to_redis_server(port)
                # 2.server is not being used by another instance of slips
                # note: using r.keys() blocks the server
                db_not_used = len(list(__database__.r.keys())) < 2
                if can_connect and db_not_used:
                    # if the db managed to connect to this random port, then this is
                    # the port we'll be using
                    return port
        except redis.exceptions.ConnectionError:
            # Connection refused to this port
            return self.generate_random_redis_port()


    def clear_redis_cache_database(self, redis_host='localhost', redis_port=6379) -> bool:
        """
        Clear cache database
        """
        rcache = redis.StrictRedis(host=redis_host, port=redis_port, db=0, charset="utf-8",
                                   decode_responses=True)
        rcache.flushdb()
        return True

    def check_zeek_or_bro(self):
        """
        Check if we have zeek or bro
        """
        if shutil.which('zeek'):
            return 'zeek'
        elif shutil.which('bro'):
            return 'bro'
        return False


    def terminate_slips(self):
        """
        Do all necessary stuff to stop process any clear any files.
        """
        if self.mode == 'daemonized':
            self.daemon.stop()
        sys.exit(-1)


    def load_modules(self, to_ignore):
        """
        Import modules and loads the modules from the 'modules' folder. Is very relative to the starting position of slips
        """

        plugins = {}
        failed_to_load_modules = 0
        # Walk recursively through all modules and packages found on the . folder.
        # __path__ is the current path of this python program
        for loader, module_name, ispkg in pkgutil.walk_packages(modules.__path__, modules.__name__ + '.'):
            if any(module_name.__contains__(mod) for mod in to_ignore):
                continue
            # If current item is a package, skip.
            if ispkg:
                continue
            # to avoid loading everything in the dir,
            # only load modules that have the same name as the dir name
            dir_name = module_name.split('.')[1]
            file_name = module_name.split('.')[2]
            if dir_name != file_name:
                continue

            # Try to import the module, otherwise skip.
            try:
                # "level specifies whether to use absolute or relative imports. The default is -1 which
                # indicates both absolute and relative imports will be attempted. 0 means only perform
                # absolute imports. Positive values for level indicate the number of parent
                # directories to search relative to the directory of the module calling __import__()."
                module = importlib.import_module(module_name)
            except ImportError as e:
                print("Something wrong happened while importing the module {0}: {1}".format(module_name, e))
                failed_to_load_modules += 1
                continue

            # Walk through all members of currently imported modules.
            for member_name, member_object in inspect.getmembers(module):
                # Check if current member is a class.
                if inspect.isclass(member_object):
                    if issubclass(member_object, Module) and member_object is not Module:
                        plugins[member_object.name] = dict(obj=member_object, description=member_object.description)

        # Change the order of the blocking module(load it first) so it can receive msgs sent from other modules
        if 'Blocking' in plugins:
            plugins = OrderedDict(plugins)
            # last=False to move to the beginning of the dict
            plugins.move_to_end('Blocking', last=False)

        return plugins, failed_to_load_modules


    def get_cwd(self):
        # Can't use os.getcwd() because slips directory name won't always be Slips plus this way requires less parsing
        for arg in sys.argv:
            if 'slips.py' in arg:
                # get the path preceeding slips.py
                # (may be ../ or  ../../ or '' if slips.py is in the cwd),
                # this path is where slips.conf will be
                cwd = arg[:arg.index('slips.py')]
                return cwd


    def prepare_zeek_scripts(self):
        """
        Adds local network to slips-conf.zeek
        """

        # get home network from slips.conf
        try:
            home_network = self.config.get('parameters', 'home_network')
        except (configparser.NoOptionError, configparser.NoSectionError, NameError):
            # There is a conf, but there is no option, or no section or no configuration file specified
            home_network = utils.home_network_ranges

        zeek_scripts_dir = os.getcwd() + '/zeek-scripts'
        # add local sites if not there
        is_local_nets_defined = False
        with open(zeek_scripts_dir + '/slips-conf.zeek', 'r') as slips_conf:
            if 'local_nets' in slips_conf.read():
                is_local_nets_defined = True

        if not is_local_nets_defined:
            with open(zeek_scripts_dir + '/slips-conf.zeek', 'a') as slips_conf:
                # update home network
                slips_conf.write('\nredef Site::local_nets += { '+home_network+' };\n')

        # # load all scripts in zeek-script dir
        # with open(zeek_scripts_dir + '/__load__.zeek','r') as f:
        #     loaded_scripts = f.read()
        # with open(zeek_scripts_dir + '/__load__.zeek','a') as f:
        #     for file_name in os.listdir(zeek_scripts_dir):
        #         # ignore the load file
        #         if file_name == '__load__.zeek':
        #             continue
        #         if file_name not in loaded_scripts:
        #             # found a file in the dir that isn't in __load__.zeek, add it
        #             f.write(f'\n@load ./{file_name}')


    def add_metadata(self):
        """
        Create a metadata dir output/metadata/ that has a copy of slips.conf, whitelist.conf, current commit and date
        """
        metadata_dir = os.path.join(self.args.output, 'metadata')
        try:
            os.mkdir(metadata_dir)
        except FileExistsError:
            # if the file exists it will be overwritten
            pass

        # Add a copy of slips.conf
        config_file = self.args.config or 'slips.conf'
        shutil.copy(config_file, metadata_dir)
        # Add a copy of whitelist.conf
        whitelist = self.config.get('parameters', 'whitelist_path')
        shutil.copy(whitelist, metadata_dir)

        branch_info = utils.get_branch_info()
        commit, branch = None, None
        if branch_info != False:
            # it's false when we're in docker because there's no .git/ there
            commit, branch = branch_info[0], branch_info[1]
        now = datetime.now()

        self.info_path = os.path.join(metadata_dir, 'info.txt')
        with open(self.info_path, 'w') as f:
            f.write(f'Slips version: {version}\n')
            f.write(f'Branch: {branch}\n')
            f.write(f'Commit: {commit}\n')
            f.write(f'Slips start date: {now}\n')

        print(f'[Main] Metadata added to {metadata_dir}')
        return self.info_path


    def shutdown_gracefully(self, input_information):
        """
        Wait for all modules to confirm that they're done processing and then shutdown
        :param input_information: the interface/pcap/nfdump/binetflow used. we need it to save the db
        """

        try:
            print('\n'+'-'*27)
            print('Stopping Slips')
            # Stop the modules that are subscribed to channels
            __database__.publish_stop()

            finished_modules = []
            # get dict of PIDs spawned by slips
            PIDs = __database__.get_PIDs()
            # we don't want to kill this process
            PIDs.pop('slips.py')
            slips_processes = len(list(PIDs.keys()))

            # Send manual stops to the processes not using channels
            for process in ('OutputProcess', 'ProfilerProcess', 'EvidenceProcess', 'InputProcess', 'logsProcess'):
                try:
                    os.kill(int(PIDs[process]), signal.SIGINT)
                except (KeyError, PermissionError):
                    # process hasn't started (for example logsProcess) so we can't send sigint
                    continue

            # only print that modules are still running once
            warning_printed = False

            # timeout variable so we don't loop forever
            # give slips enough time to close all modules - make sure all modules aren't considered 'busy' when slips stops
            max_loops = 430
            # loop until all loaded modules are finished
            try:
                while len(finished_modules) < slips_processes and max_loops != 0:
                    # print(f"Modules not finished yet {set(loaded_modules) - set(finished_modules)}")
                    try:
                        message = self.c1.get_message(timeout=0.01)
                    except NameError:
                        # Sometimes the c1 variable does not exist yet. So just force the shutdown
                        message = {
                            'data': 'dummy_value_not_stopprocess',
                            'channel': 'finished_modules'}

                    if message and message['data'] == 'stop_process':
                        continue
                    if message and message['channel'] == 'finished_modules' and type(message['data']) == str:
                        # all modules must reply with their names in this channel after
                        # receiving the stop_process msg
                        # to confirm that all processing is done and we can safely exit now
                        module_name = message['data']

                        if module_name not in finished_modules:
                            finished_modules.append(module_name)
                            try:
                                # remove module from the list of opened pids
                                PIDs.pop(module_name)
                            except KeyError:
                                # reaching this block means a module that belongs to slips
                                # is publishing in  finished_modules
                                # but slips doesn't know of it's PID!!
                                print(f"[Main] Module{module_name} just published in "
                                      f"finished_modules channel and Slips doesn't know about it's PID!", 0, 1)
                                # pass insead of continue because
                                # this module is finished and we need to print that it has stopped
                                pass
                            modules_left = len(list(PIDs.keys()))
                            # to vertically align them when printing
                            module_name = module_name + ' '*(20-len(module_name))
                            print(f"\t\033[1;32;40m{module_name}\033[00m \tStopped. \033[1;32;40m{modules_left}\033[00m left.")
                    max_loops -= 1
                    # after reaching the max_loops and before killing the modules that aren't finished,
                    # make sure we're not in the middle of processing
                    if len(PIDs) > 0 and max_loops < 2:
                        if not warning_printed:
                            # some modules publish in finished_modules channel before slips.py starts listening,
                            # but they finished gracefully.
                            # remove already stopped modules from PIDs dict
                            for module, pid in PIDs.items():
                                try:
                                    # signal 0 is used to check if the pid exists
                                    os.kill(int(pid), 0)
                                except ProcessLookupError:
                                    # pid doesn't exist because module already stopped
                                    finished_modules.append(module)

                            # exclude the module that are already stopped from the pending modules
                            pending_modules = [module for module in list(PIDs.keys()) if module not in finished_modules]
                            if len(pending_modules) > 0:
                                print(f"\n[Main] The following modules are busy working on your data."
                                      f"\n\n{pending_modules}\n\n"
                                      "You can wait for them to finish, or you can press CTRL-C again to force-kill.\n")
                                warning_printed = True
                        # -P flag is only used in integration tests,
                        # so we don't care about the modules finishing their job when testing
                        # instead, kill them
                        if not self.args.port:
                            # delay killing unstopped modules
                            max_loops += 1
                            continue
            except KeyboardInterrupt:
                # either the user wants to kill the remaining modules (pressed ctrl +c again)
                # or slips was stuck looping for too long that the os sent an automatic sigint to kill slips
                # pass to kill the remaining modules
                pass


            # modules that aren't subscribed to any channel will always be killed and not stopped
            # comes here if the user pressed ctrl+c again
            for unstopped_proc, pid in PIDs.items():
                unstopped_proc = unstopped_proc+' '*(20-len(unstopped_proc))
                try:
                    os.kill(int(pid), 9)
                    print(f'\t\033[1;32;40m{unstopped_proc}\033[00m \tKilled.')
                except ProcessLookupError:
                    print(f'\t\033[1;32;40m{unstopped_proc}\033[00m \tAlready stopped.')


            # save redis database if '-s' is specified
            if self.args.save:
                # Create a new dir to store backups
                backups_dir = self.get_cwd() + 'redis_backups' + '/'
                try:
                    os.mkdir(backups_dir)
                except FileExistsError:
                    pass
                # The name of the interface/pcap/nfdump/binetflow used is in input_information
                # if the input is a zeek dir, remove the / at the end
                if input_information.endswith('/'):
                    input_information = input_information[:-1]
                # We need to separate it from the path
                input_information = os.path.basename(input_information)
                # Remove the extension from the filename
                try:
                    input_information = input_information[:input_information.index('.')]
                except ValueError:
                    # it's a zeek dir
                    pass
                # Give the exact path to save(), this is where our saved .rdb backup will be
                __database__.save(backups_dir + input_information)
                # info will be lost only if you're out of space and redis can't write to dump.rdb, otherwise you're fine
                print("[Main] [Warning] stop-writes-on-bgsave-error is set to no, information may be lost in the redis backup file.")

            # make sure that redis isn't saving snapshots whether -s is given or not
            __database__.disable_redis_snapshots()

            # if store_a_copy_of_zeek_files is set to yes in slips.conf, copy the whole zeek_files dir to the output dir
            try:
                store_a_copy_of_zeek_files = self.config.get('parameters', 'store_a_copy_of_zeek_files')
                store_a_copy_of_zeek_files = False if 'no' in store_a_copy_of_zeek_files.lower() else True
            except (configparser.NoOptionError, configparser.NoSectionError, NameError):
                # There is a conf, but there is no option, or no section or no configuration file specified
                store_a_copy_of_zeek_files = False

            if store_a_copy_of_zeek_files:
                # this is where the copy will be stores
                zeek_files_path = os.path.join(self.args.output, 'zeek_files')
                copy_tree("zeek_files", zeek_files_path)
                print(f"[Main] Stored a copy of zeek files to {zeek_files_path}.")

            # if delete_zeek_files is set to yes in slips.conf,
            # delete the whole zeek_files
            try:
                delete_zeek_files = self.config.get('parameters', 'delete_zeek_files')
                delete_zeek_files = False if 'no' in delete_zeek_files.lower() else True
            except (configparser.NoOptionError, configparser.NoSectionError, NameError):
                # There is a conf, but there is no option, or no section or no configuration file specified
                delete_zeek_files = True

            if delete_zeek_files:
                shutil.rmtree('zeek_files')
            # add slips end date in the metadata dir
            try:
                if 'yes' in self.enable_metadata.lower():
                    with open(self.info_path, 'a') as f:
                        now = datetime.now()
                        f.write(f'Slips end date: {now}\n')
            except (NameError, AttributeError):
                # slips is shut down before enable_metadata is read from slips.conf
                pass
            if self.mode == 'daemonized':
                profilesLen = str(__database__.getProfilesLen())
                print(f"Total Number of Profiles in DB: {profilesLen}.")
                self.daemon.stop()
            os._exit(-1)
            return True
        except KeyboardInterrupt:
            return False

    def get_open_servers_PIDs(self) -> dict:
        """
        Returns the dict of PIDs and ports of the redis unused servers started by slips
        """
        self.open_servers_PIDs = {}

        with open(self.used_redis_servers, 'r') as f:
            for line in f.read().splitlines():
                # skip comments
                if (line.startswith('#')
                        or line.startswith('Date')
                        or len(line) < 3):
                    continue
                line = re.split(r'\s{2,}', line)

                pid, port = line[-1], line[-2]
                self.open_servers_PIDs[pid] = port
        return self.open_servers_PIDs

    def close_open_redis_servers(self):
        """ Function to warn about unused open redis-servers """
        if not hasattr(self, 'open_servers_PIDs'):
            # fill the dict
            self.get_open_servers_PIDs()

        if len(self.open_servers_PIDs) == 0:
            print("No unused open servers to kill.")
            sys.exit(-1)
            return

        for pid, port in self.open_servers_PIDs.items():
            if pid == 'Not Found':
                # The server was killed before logging its PID
                continue

            # clear the server opened on this port
            connected = __database__.connect_to_redis_server(port)
            if connected:
                __database__.r.flushall()
                __database__.r.script_flush()

            # signal 0 is to check if the process is still running or not
            # it returns 1 if the process exited
            try:
                # check if the process is still running
                while os.kill(int(pid), 0) != 1:
                    # sigterm is 9
                    os.kill(int(pid), 9)
            except (ProcessLookupError, PermissionError):
                # process already exited, sometimes this exception is raised
                # but the process is still running, keep trying to kill it
                # PermissionError happens when the user tries to close redis-servers
                # opened by root while he's not root,
                # or when he tries to close redis-servers
                # opened without root while he's root
                continue

        print(f"Killed {len(self.open_servers_PIDs.keys())} Redis Servers.")
        # delete the closed redis servers from used_redis_servers.txt
        with open(self.used_redis_servers, 'w') as f:
            f.write("# This file contains a list of used redis ports.\n"
                    "# Once a server is killed, it will be removed from this file.\n"
                    "Date                   File or interface                   Used port       Server PID\n")
        sys.exit(-1)



    def is_debugger_active(self) -> bool:
        """Return if the debugger is currently active"""
        gettrace = getattr(sys, 'gettrace', lambda: None)
        return gettrace() is not None

    def prepare_output_dir(self, input_information):
        """
        :param input_information: either an interface or a filename (wlp3s0, sample.pcap, etc.)
        """

        if self.args.output == self.alerts_default_path:
            # now that slips can run several instances,
            # each created dir will be named after the instance
            # that created it
            self.args.output += f'{input_information.split("/")[-1]}'
        else:
            # -o is given
            # Create output folder for alerts.log and
            # alerts.json if it doesn't  exist
            if not self.args.output.endswith('/'):
                self.args.output = self.args.output + '/'
            try:
                os.remove(self.args.output + 'alerts.log')
                os.remove(self.args.output + 'alerts.json')
            except OSError:
                # they weren't created in the first place
                pass
            return

        # this ctr will be appended to the dir name,
        # so we don't overwrite existing log dirs
        while os.path.exists(self.args.output):
            # delete the / at the end
            if self.args.output.endswith('/'):
                self.args.output = self.args.output[:-1]

            try:
                # if the dir ends with a ctr, delete the ctr so we can replace it
                dir_ends_with = int(self.args.output[-1])
                # it ends with _ctr, remove the _ and the ctr
                self.args.output = self.args.output[:-2]
                dir_ends_with += 1
                self.args.output += f'_{dir_ends_with}/'
                continue
            except ValueError:
                # found an existing dir that doesn't end with a ctr
                self.args.output += '_1/'

        if not self.args.output.endswith('/'):
            self.args.output = self.args.output + '/'

        if not os.path.exists(self.args.output):
            os.makedirs(self.args.output)



        print(f"[Main] storing Slips logs in {self.args.output}")

    def parse_arguments(self):
        # Parse the parameters
        slips_conf_path = self.get_cwd() + 'slips.conf'
        parser = ArgumentParser(usage="./slips.py -c <configfile> [options] [file]",
                                add_help=False)
        parser.add_argument('-c', '--config', metavar='<configfile>',
                            action='store', required=False, default=slips_conf_path,
                            help='Path to the Slips config file.')
        parser.add_argument('-v', '--verbose', metavar='<verbositylevel>', action='store', required=False, type=int,
                            help='Verbosity level. This logs more info about slips.')
        parser.add_argument('-e', '--debug', metavar='<debuglevel>', action='store', required=False, type=int,
                            help='Debugging level. This shows more detailed errors.')
        parser.add_argument('-f', '--filepath', metavar='<file>', action='store', required=False,
                            help='read a Zeek folder, Argus binetflow, pcapfile or nfdump.')
        parser.add_argument('-i', '--interface', metavar='<interface>', action='store', required=False,
                            help='Read packets from an interface.')
        parser.add_argument('-l', '--createlogfiles', action='store_true', required=False,
                            help='Create log files with all the traffic info and detections.')
        parser.add_argument('-F', '--pcapfilter', action='store', required=False, type=str,
                            help='packet filter for Zeek. BPF style.')
        parser.add_argument('-G',  '--gui', help='Use the GUI interface.',
                            required=False, default=False, action='store_true')
        parser.add_argument('-cc', '--clearcache', action='store_true',
                            required=False, help='Clear the cache database.')
        parser.add_argument('-p', '--blocking',
                            help='Allow Slips to block malicious IPs. Requires root access. Supported only on Linux.',
                            required=False, default=False, action='store_true')
        parser.add_argument('-cb', '--clearblocking', help='Flush and delete slipsBlocking iptables chain',
                            required=False, default=False, action='store_true')
        parser.add_argument('-o', '--output', action='store', required=False, default=self.alerts_default_path,
                            help='Store alerts.json and alerts.txt in the provided folder.')
        parser.add_argument('-s', '--save', action='store_true', required=False,
                            help='To Save redis db to disk. Requires root access.')
        parser.add_argument('-d', '--db', action='store', required=False,
                            help='To read a redis (rdb) saved file. Requires root access.')
        parser.add_argument('-D', '--daemon',required=False, default=False, action='store_true',
                            help="run slips in daemon mode, not interactive")
        parser.add_argument('-S', '--stopdaemon',required=False, default=False, action='store_true',
                            help="Stop slips daemon")
        parser.add_argument('-R', '--restartdaemon',required=False, default=False, action='store_true',
                            help="Restart slips daemon")
        parser.add_argument('-k', '--killall', action='store_true', required=False,
                            help='Kill all unused redis servers')
        parser.add_argument('-P', '--port', action='store', required=False,
                            help='The redis-server port to use')
        parser.add_argument("-h", "--help", action="help", help="command line help")

        self.args = parser.parse_args()

    def read_conf_file(self):
        # Read the config file name given from the parameters
        # don't use '%' for interpolation.
        self.config = configparser.ConfigParser(interpolation=None)
        try:
            with open(self.args.config) as source:
                self.config.read_file(source)
        except (IOError, TypeError):
            pass

        return self.config

    def log_redis_server_PID(self, redis_port, input_information):
        """
        get the PID of the redis server started on the given redis_port
        and logs it in used_redis_servers.txt
        """
        # log the pid of the redis server using this port
        redis_pid = 'Not found'
        # On modern systems, the netstat utility comes pre-installed,
        # this can be done using psutil but it needs root on macos
        command = f'netstat -peanut'
        if self.mode == 'daemonized':
            # A pty is a pseudo-terminal - it's a software implementation that appears to
            # the attached program like a terminal, but instead of communicating
            # directly with a "real" terminal, it transfers the input and output to another program.
            master, slave = pty.openpty()
            # connect the slave to the pty, and transfer from slave to master
            subprocess.Popen(command, shell=True, stdin=subprocess.PIPE, stdout=slave, stderr=slave, close_fds=True)
            # connect the master to slips
            cmd_output = os.fdopen(master)
        else:
            command = f'netstat -peanut'
            result = subprocess.run(command.split(), capture_output=True)
            # Get command output
            cmd_output = result.stdout.decode('utf-8').splitlines()

        for line in cmd_output:
            if f":{redis_port}" in line and "redis-server" in line:
                line = re.split(r'\s{2,}', line)
                # get the substring that has the pid
                try:
                    redis_pid = line[-1]
                    _ = redis_pid.index('/')
                except ValueError:
                    redis_pid = line[-2]
                redis_pid = redis_pid.split('/')[0]
                break
        # log redis-server pid
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with open(self.used_redis_servers, 'a') as f:
            f.write(f'{now: <16}    {input_information: <35}    {redis_port: <6}        {redis_pid}\n')

    def set_mode(self, mode, daemon=''):
        """
        Slips has 2 modes, daemonized and interactive, this function sets up the mode so that slips knows in which mode it's operating
        :param mode: daemonized of interavtive
        :param daemon: Daemon() instance
        """
        self.mode = mode
        self.daemon = daemon

    def start(self):
        """ Main Slips Function """
        try:
            slips_version = f'Slips. Version {version}'
            from slips_files.common.slips_utils import utils
            branch_info = utils.get_branch_info()
            if branch_info != False:
                # it's false when we're in docker because there's no .git/ there
                commit = branch_info[0]
                slips_version += f' ({commit[:8]})'

            print(slips_version)
            print('https://stratosphereips.org')
            print('-'*27)

            self.read_conf_file()

            if (self.args.verbose and int(self.args.verbose) > 3) or (self.args.debug and int(self.args.debug) > 3):
                print("Debug and verbose values range from 0 to 3.")
                self.terminate_slips()

            # Check if redis server running
            if self.check_redis_database() is False:
                print("Redis database is not running. Stopping Slips")
                self.terminate_slips()

            # Clear cache if the parameter was included
            if self.args.clearcache:
                print('Deleting Cache DB in Redis.')
                self.clear_redis_cache_database()
                self.terminate_slips()

            # kill all open unused redis servers if the parameter was included
            if self.args.killall:
                self.close_open_redis_servers()
                self.terminate_slips()


            if self.args.clearblocking:
                if os.geteuid() != 0:
                    print("Slips needs to be run as root to clear the slipsBlocking chain. Stopping.")
                    self.terminate_slips()
                else:
                    # start only the blocking module process and the db
                    from slips_files.core.database import __database__
                    from multiprocessing import Queue
                    from modules.blocking.blocking import Module
                    blocking = Module(Queue(), self.config)
                    blocking.start()
                    blocking.delete_slipsBlocking_chain()
                    # Tell the blocking module to clear the slips chain
                    self.shutdown_gracefully('')

            # Check if user want to save and load a db at the same time
            if self.args.save:
                # make sure slips is running as root
                if os.geteuid() != 0:
                    print("Slips needs to be run as root to save the database. Stopping.")
                    self.terminate_slips()
                if self.args.db:
                    print("Can't use -s and -b together")
                    self.terminate_slips()

            line_type = False
            # Check the type of input
            if self.args.interface:
                input_information = self.args.interface
                input_type = 'interface'
            elif self.args.filepath:
                input_information = self.args.filepath
                # check invalid file path
                if not os.path.exists(input_information) and input_information != 'stdin':
                    print(f'[Main] Invalid file path {input_information}. Stopping.')
                    os._exit(-1)

                if input_information != 'stdin':
                    # default value
                    input_type = 'file'
                    # Get the type of file
                    cmd_result = subprocess.run(['file', input_information], stdout=subprocess.PIPE)
                    # Get command output
                    cmd_result = cmd_result.stdout.decode('utf-8')

                    if 'pcap' in cmd_result:
                        input_type = 'pcap'
                    elif 'dBase' in cmd_result:
                        input_type = 'nfdump'
                        if shutil.which('nfdump') == None:
                            # If we do not have nfdump, terminate Slips.
                            print("nfdump is not installed. terminating slips.")
                            self.terminate_slips()
                    elif 'CSV' in cmd_result:
                        input_type = 'binetflow'
                    elif 'directory' in cmd_result:
                        input_type = 'zeek_folder'
                    else:
                        # is it a zeek log file or suricata, binetflow tabs , or binetflow comma separated file?
                        # use first line to determine
                        with open(input_information, 'r') as f:
                            while True:
                                # get the first line that isn't a comment
                                first_line = f.readline().replace('\n', '')
                                if not first_line.startswith('#'):
                                    break
                        if 'flow_id' in first_line:
                            input_type = 'suricata'
                        else:
                            # this is a text file , it can be binetflow or zeek_log_file
                            try:
                                # is it a json log file
                                json.loads(first_line)
                                input_type = 'zeek_log_file'
                            except json.decoder.JSONDecodeError:
                                # this is a tab separated file
                                # is it zeek log file or binetflow file?
                                # line = re.split(r'\s{2,}', first_line)[0]
                                tabs_found = re.search('\s{1,}-\s{1,}', first_line)
                                if '->' in first_line or 'StartTime' in first_line:
                                    # tab separated files are usually binetflow tab files
                                    input_type = 'binetflow-tabs'
                                elif tabs_found:
                                    input_type = 'zeek_log_file'
                elif self.mode == 'interactive':
                    input_type = 'stdin'
                    line_type = input("Enter the flow type, available options are:\nargus, argus-tabs, suricata, zeek, zeek-tabs or nfdump\n")
                    if line_type.lower() not in ('argus', 'argus-tabs', 'suricata', 'zeek', 'zeek-tabs', 'nfdump'):
                        print(f"[Main] invalid line type {line_type}")
                        sys.exit(-1)
                else:
                    print("Slips can't take input from stdin while in daemonized mode. Start slips again with -I")
                    sys.exit(-1)
            elif self.args.db:
                input_type = 'database'
                input_information = 'database'
                from slips_files.core.database import __database__
                __database__.start(self.config, 6379)
                if not __database__.load(self.args.db):
                    print(f'Error loading the database {self.args.db}.')
                else:
                    print(f"{self.args.db} loaded successfully. Run ./kalipso.sh")
                self.terminate_slips()

            else:
                print('[Main] You need to define an input source.')
                sys.exit(-1)


            # If we need zeek (bro), test if we can run it.
            # Need to be assign to something because we pass it to inputProcess later
            zeek_bro = None
            if input_type in ('pcap', 'interface'):
                zeek_bro = self.check_zeek_or_bro()
                if not zeek_bro:
                    # If we do not have bro or zeek, terminate Slips.
                    print('Error. No zeek or bro binary found.')
                    self.terminate_slips()
                else:
                    self.prepare_zeek_scripts()

            # Remove default alerts files, if exists, don't remove if we're stopping the daemon
            # set alerts.log and alerts.json default paths,
            # using argparse default= will cause files to be stored in output/ dir even in daemonized mode

            if not self.args.stopdaemon:
                self.prepare_output_dir(input_information)

                # Also check if the user blocks on interface, does not make sense to block on files
                if self.args.interface and self.args.blocking and os.geteuid() != 0:
                    # If the user wants to blocks,we need permission to modify iptables
                    print('[Main] Run Slips with sudo to enable the blocking module.')
                    self.shutdown_gracefully(input_information)

                """
                Import modules here because if user wants to run "./slips.py --help" it should never throw error. 
                """
                from multiprocessing import Queue
                from slips_files.core.inputProcess import InputProcess
                from slips_files.core.outputProcess import OutputProcess
                from slips_files.core.profilerProcess import ProfilerProcess
                from slips_files.core.guiProcess import GuiProcess
                from slips_files.core.logsProcess import LogsProcess
                from slips_files.core.evidenceProcess import EvidenceProcess

                # Any verbosity passed as parameter overrides the configuration. Only check its value
                if self.args.verbose == None:
                    # Read the verbosity from the config
                    try:
                        self.args.verbose = int(self.config.get('parameters', 'verbose'))
                    except (configparser.NoOptionError, configparser.NoSectionError, NameError, ValueError):
                        # There is a conf, but there is no option, or no section or no configuration file specified
                        # By default, 1
                        self.args.verbose = 1

                # Limit any verbosity to > 0
                if self.args.verbose < 1:
                    self.args.verbose = 1

                # Any debuggsity passed as parameter overrides the configuration. Only check its value
                if self.args.debug == None:
                    # Read the debug from the config
                    try:
                        self.args.debug = int(self.config.get('parameters', 'debug'))
                    except (configparser.NoOptionError, configparser.NoSectionError, NameError, ValueError):
                        # There is a conf, but there is no option, or no section or no configuration file specified
                        # By default, 0
                        self.args.debug = 0

                # Limit any debuggisity to > 0
                if self.args.debug < 0:
                    self.args.debug = 0
                ##########################
                # Creation of the threads
                ##########################
                from slips_files.core.database import __database__
                # get the port that is going to be used for this instance of slips
                if self.args.port:
                    redis_port = int(self.args.port)
                else:
                    redis_port = self.generate_random_redis_port()


                # Output thread. This thread should be created first because it handles
                # the output of the rest of the threads.
                # Create the queue
                outputProcessQueue = Queue()
                # if stdout it redirected to a file, tell outputProcess.py to redirect it's output as well
                # lsof will provide a list of all open fds belonging to slips
                command = f'lsof -p {os.getpid()}'
                result = subprocess.run(command.split(), capture_output=True)
                # Get command output
                output = result.stdout.decode('utf-8')
                # if stdout is being redirected we'll find '1w' in one of the lines 1 means stdout, w means write mode
                for line in output.splitlines():
                    if '1w' in line:
                        # stdout is redirected, get the file
                        current_stdout = line.split(' ')[-1]
                        break
                else:
                    # stdout is not redirected
                    current_stdout = ''
                # stderr is redirected when daemonized, tell outputprocess
                stderr = f'output/{input_information.split("/")[-1]}/errors.log'
                if self.args.output:
                    stderr = f'output/errors.log'
                if self.mode == 'daemonized':
                    stderr = self.daemon.stderr

                # Create the output thread and start it
                outputProcessThread = OutputProcess(outputProcessQueue,
                                                    self.args.verbose,
                                                    self.args.debug,
                                                    self.config,
                                                    redis_port,
                                                    stdout=current_stdout,
                                                    stderr=stderr)
                # this process starts the db
                outputProcessThread.start()

                if self.args.save:
                    __database__.enable_redis_snapshots()
                else:
                    __database__.disable_redis_snapshots()

                # now that we have successfully connected to the db,
                # log the PID of the started redis-server
                self.log_redis_server_PID(redis_port, input_information)
                outputProcessQueue.put(f'10|main|Using redis server on port: {redis_port}')

                # Before starting update malicious file
                # create an event loop and allow it to run the update_file_manager asynchronously
                # Print the PID of the main slips process. We do it here because we needed the queue to the output process
                outputProcessQueue.put('10|main|Started main program [PID {}]'.format(os.getpid()))
                # Output pid
                __database__.store_process_PID('OutputProcess', int(outputProcessThread.pid))

                outputProcessQueue.put('10|main|Started output thread [PID {}]'.format(outputProcessThread.pid))

                # Start each module in the folder modules
                outputProcessQueue.put('01|main|Starting modules')
                to_ignore = self.read_configuration(self.config, 'modules', 'disable')
                use_p2p = self.read_configuration(self.config, 'P2P', 'use_p2p')


                # This plugins import will automatically load the modules and put them in the __modules__ variable
                # if slips is given a .rdb file, don't load the modules as we don't need them
                if to_ignore and not self.args.db:
                    # Convert string to list
                    to_ignore = to_ignore.replace("[", "").replace("]", "").replace(" ", "").split(",")
                    # Ignore exporting alerts module if export_to is empty
                    export_to = self.config.get('ExportingAlerts', 'export_to').rstrip("][").replace(" ", "").lower()
                    if 'stix' not in export_to and 'slack' not in export_to and 'json' not in export_to:
                        to_ignore.append('exporting_alerts')
                    if not use_p2p or use_p2p == 'no' or not self.args.interface:
                        to_ignore.append('p2ptrust')

                    # ignore CESNET sharing module if send and receive are are disabled in slips.conf
                    send_to_warden = self.config.get('CESNET', 'send_alerts').lower()
                    receive_from_warden = self.config.get('CESNET', 'receive_alerts').lower()
                    if 'no' in send_to_warden and 'no' in receive_from_warden:
                        to_ignore.append('CESNET')
                    # don't run blocking module unless specified
                    if not self.args.clearblocking and not self.args.blocking \
                            or (self.args.blocking and not self.args.interface):  # ignore module if not using interface
                        to_ignore.append('blocking')

                    # leak detector only works on pcap files
                    if input_type != 'pcap':
                        to_ignore.append('leak_detector')

                    # Import all the modules
                    modules_to_call = self.load_modules(to_ignore)[0]
                    for module_name in modules_to_call:
                        if module_name not in to_ignore:
                            module_class = modules_to_call[module_name]['obj']
                            ModuleProcess = module_class(outputProcessQueue, self.config, redis_port)
                            ModuleProcess.start()
                            __database__.store_process_PID(module_name, int(ModuleProcess.pid))
                            description = modules_to_call[module_name]['description']
                            outputProcessQueue.put(
                                f'10|main|\t\tStarting the module {module_name} '
                                f'({description}) '
                                f'[PID {ModuleProcess.pid}]')


                # Get the type of output from the parameters
                # Several combinations of outputs should be able to be used
                # if self.args.gui:
                #     # Create the curses thread
                #     guiProcessQueue = Queue()
                #     guiProcessThread = GuiProcess(guiProcessQueue, outputProcessQueue, self.args.verbose, self.args.debug, self.config)
                #     guiProcessThread.start()
                #     outputProcessQueue.put('quiet')

                do_logs = self.read_configuration(self.config, 'parameters', 'create_log_files')
                # if -l is provided or create_log_files is yes then we will create log files
                if self.args.createlogfiles or do_logs == 'yes':
                    # Create a folder for logs
                    logs_folder = self.create_folder_for_logs()
                    # Create the logsfile thread if by parameter we were told, or if it is specified in the configuration
                    logsProcessQueue = Queue()
                    logsProcessThread = LogsProcess(logsProcessQueue, outputProcessQueue,
                                                    self.args.verbose, self.args.debug, self.config, logs_folder)
                    logsProcessThread.start()
                    outputProcessQueue.put('10|main|Started logsfiles thread [PID {}]'.format(logsProcessThread.pid))
                    __database__.store_process_PID('logsProcess', int(logsProcessThread.pid))
                else:
                    # If self.args.nologfiles is False, then we don't want log files, independently of what the conf says.
                    logs_folder = False

                # Evidence thread
                # Create the queue for the evidence thread
                evidenceProcessQueue = Queue()
                # Create the thread and start it
                evidenceProcessThread = EvidenceProcess(evidenceProcessQueue,
                                                        outputProcessQueue,
                                                        self.config,
                                                        self.args.output,
                                                        logs_folder,
                                                        redis_port)
                evidenceProcessThread.start()
                outputProcessQueue.put('10|main|Started Evidence thread [PID {}]'.format(evidenceProcessThread.pid))
                __database__.store_process_PID('EvidenceProcess', int(evidenceProcessThread.pid))
                __database__.store_process_PID('slips.py', int(os.getpid()))

                # Profile thread
                # Create the queue for the profile thread
                profilerProcessQueue = Queue()
                # Create the profile thread and start it
                profilerProcessThread = ProfilerProcess(profilerProcessQueue,
                                                        outputProcessQueue, self.args.verbose, self.args.debug, self.config, redis_port)
                profilerProcessThread.start()
                outputProcessQueue.put('10|main|Started Profiler thread [PID {}]'.format(profilerProcessThread.pid))
                __database__.store_process_PID('ProfilerProcess', int(profilerProcessThread.pid))

                self.c1 = __database__.subscribe('finished_modules')

                # Input process
                # Create the input process and start it
                inputProcess = InputProcess(outputProcessQueue,
                                            profilerProcessQueue,
                                            input_type,
                                            input_information,
                                            self.config,
                                            self.args.pcapfilter,
                                            zeek_bro,
                                            line_type,
                                            redis_port)
                inputProcess.start()
                outputProcessQueue.put('10|main|Started input thread [PID {}]'.format(inputProcess.pid))
                __database__.store_process_PID('InputProcess', int(inputProcess.pid))

                # warn about unused open redis servers
                open_servers = len(self.get_open_servers_PIDs())
                if open_servers > 1:
                    print(f"[Main] Warning: You have {open_servers} redis servers running. "
                          f"Run Slips with --killall to stop them.")

                self.enable_metadata = self.read_configuration(self.config, 'parameters', 'metadata_dir')
                if 'yes' in self.enable_metadata.lower():
                    self.info_path = self.add_metadata()

                # Store the host IP address if input type is interface
                if input_type == 'interface':
                    hostIP = self.recognize_host_ip()
                    while True:
                        try:
                            __database__.set_host_ip(hostIP)
                            break
                        except redis.exceptions.DataError:
                            print("Not Connected to the internet. Reconnecting in 10s.")
                            time.sleep(10)
                            hostIP = self.recognize_host_ip()

                # As the main program, keep checking if we should stop slips or not
                # This is not easy since we need to be sure all the modules are stopped
                # Each interval of checking is every 5 seconds
                check_time_sleep = 5
                # In each interval we check if there has been any modifications to the database by any module.
                # If not, wait this amount of intervals and then stop slips.
                # We choose 6 to wait 30 seconds.
                limit_minimum_intervals_to_wait = 4
                minimum_intervals_to_wait = limit_minimum_intervals_to_wait
                fieldseparator = __database__.getFieldSeparator()
                slips_internal_time = 0
                try:
                    while True:
                        # Sleep some time to do rutine checks
                        time.sleep(check_time_sleep)
                        slips_internal_time = float(__database__.getSlipsInternalTime())+1
                        # Get the amount of modified profiles since we last checked
                        modified_profiles, last_modified_tw_time = __database__.getModifiedProfilesSince(slips_internal_time)
                        amount_of_modified = len(modified_profiles)
                        # Get the time of last modified timewindow and set it as a new
                        if last_modified_tw_time != 0:
                            __database__.setSlipsInternalTime(last_modified_tw_time)
                        # How many profiles we have?
                        profilesLen = str(__database__.getProfilesLen())
                        if self.mode != 'daemonized':
                            print(f'Total Number of Profiles in DB so far: {profilesLen}. '
                                      f'Modified Profiles in the last TW: {amount_of_modified}. '
                                      f'({datetime.now().strftime("%Y-%m-%d--%H:%M:%S")})', end='\r')

                        # Check if we need to close some TW
                        __database__.check_TW_to_close()

                        # In interface we keep track of the host IP. If there was no
                        # modified TWs in the host NotIP, we check if the network was changed.
                        # Don't try to stop slips if it's capturing from an interface
                        if self.args.interface:
                            # To check of there was a modified TW in the host IP. If not,
                            # count down.
                            modifiedTW_hostIP = False
                            for profileIP in modified_profiles:
                                # True if there was a modified TW in the host IP
                                if hostIP == profileIP:
                                    modifiedTW_hostIP = True

                            # If there was no modified TW in the host IP
                            # then start counting down
                            # After count down we update the host IP, to check if the
                            # network was changed
                            if not modifiedTW_hostIP and self.args.interface:
                                if minimum_intervals_to_wait == 0:
                                    hostIP = self.recognize_host_ip()
                                    if hostIP:
                                        __database__.set_host_ip(hostIP)
                                    minimum_intervals_to_wait = limit_minimum_intervals_to_wait
                                minimum_intervals_to_wait -= 1
                            else:
                                minimum_intervals_to_wait = limit_minimum_intervals_to_wait

                        # Running Slips in the file.
                        # If there were no modified TW in the last timewindow time,
                        # then start counting down
                        else:
                            # don't shutdown slips if it's being debugged or reading flows from stdin
                            if (amount_of_modified == 0
                                    and not self.is_debugger_active()
                                    and input_type != 'stdin'):
                                # print('Counter to stop Slips. Amount of modified
                                # timewindows: {}. Stop counter: {}'.format(amount_of_modified, minimum_intervals_to_wait))
                                if minimum_intervals_to_wait == 0:
                                    self.shutdown_gracefully(input_information)
                                    break
                                minimum_intervals_to_wait -= 1
                            else:
                                minimum_intervals_to_wait = limit_minimum_intervals_to_wait

                        __database__.pubsub.check_health()

                except KeyboardInterrupt:
                    self.shutdown_gracefully(input_information)

        except KeyboardInterrupt:
            # the EINTR error code happens if a signal occurred while the system call was in progress
            # comes here if zeek terminates while slips is still working
            self.shutdown_gracefully(input_information)

####################
# Main
####################
if __name__ == '__main__':
    slips = Main()
    slips.parse_arguments()
    # if any one of the following args are given, don't start the daemon
    start_interactive = [slips.args.clearcache,
                         slips.args.killall,
                         slips.args.clearblocking]
    if any(start_interactive) or not slips.args.daemon:
        slips.start()
        sys.exit()

    daemon = Daemon(slips)
    if slips.args.stopdaemon:
        # -S is provided
        print("Daemon stopped.")
        daemon.stop()
    elif slips.args.restartdaemon:
        # -R is provided
        print("Daemon restarted.")
        daemon.restart()
    else:
        # Default mode (daemonized)
        print("Slips daemon started.")
        daemon.start()
