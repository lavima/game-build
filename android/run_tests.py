#!/usr/bin/env python
# Copyright (c) 2012 The Chromium Authors. All rights reserved.
# Use of this source code is governed by a BSD-style license that can be
# found in the LICENSE file.

"""Runs all the native unit tests.

1. Copy over test binary to /data/local on device.
2. Resources: chrome/unit_tests requires resources (chrome.pak and en-US.pak)
   to be deployed to the device (in /data/local/tmp).
3. Environment:
3.1. chrome/unit_tests requires (via chrome_paths.cc) a directory named:
     /data/local/tmp/chrome/test/data
3.2. page_cycler_tests have following requirements,
3.2.1  the following data on host:
       <chrome_src_dir>/tools/page_cycler
       <chrome_src_dir>/data/page_cycler
3.2.2. two data directories to store above test data on device named:
       /data/local/tmp/tools/ (for database perf test)
       /data/local/tmp/data/ (for other perf tests)
3.2.3. a http server to serve http perf tests.
       The http root is host's <chrome_src_dir>/data/page_cycler/, port 8000.
3.2.4  a tool named forwarder is also required to run on device to
       forward the http request/response between host and device.
3.2.5  Chrome is installed on device.
4. Run the binary in the device and stream the log to the host.
4.1. Optionally, filter specific tests.
4.2. Optionally, rebaseline: run the available tests and update the
     suppressions file for failures.
4.3. If we're running a single test suite and we have multiple devices
     connected, we'll shard the tests.
5. Clean up the device.

Suppressions:

Individual tests in a test binary can be suppressed by listing it in
the gtest_filter directory in a file of the same name as the test binary,
one test per line. Here is an example:

  $ cat gtest_filter/base_unittests_disabled
  DataPackTest.Load
  ReadOnlyFileUtilTest.ContentsEqual

This file is generated by the tests running on devices. If running on emulator,
additonal filter file which lists the tests only failed in emulator will be
loaded. We don't care about the rare testcases which succeeded on emuatlor, but
failed on device.
"""

import fnmatch
import logging
import multiprocessing
import os
import re
import subprocess
import sys
import time

import android_commands
from base_test_sharder import BaseTestSharder
import cmd_helper
import debug_info
import emulator
import run_tests_helper
from single_test_runner import SingleTestRunner
from test_package_executable import TestPackageExecutable
from test_result import BaseTestResult, TestResults

_TEST_SUITES = ['base_unittests',
                'content_unittests',
                'gpu_unittests',
                'ipc_tests',
                'net_unittests',
                'sql_unittests',
                'sync_unit_tests',
                'ui_unittests',
               ]


def FullyQualifiedTestSuites(apk):
  """Return a fully qualified list that represents all known suites.

  Args:
    apk: if True, use the apk-based test runner"""
  # If not specified, assume the test suites are in out/Release
  test_suite_dir = os.path.abspath(os.path.join(run_tests_helper.CHROME_DIR,
                                                'out', 'Release'))
  if apk:
    # out/Release/$SUITE_apk/ChromeNativeTests-debug.apk
    suites = [os.path.join(test_suite_dir,
                           t + '_apk',
                           'ChromeNativeTests-debug.apk')
              for t in _TEST_SUITES]
  else:
    suites = [os.path.join(test_suite_dir, t) for t in _TEST_SUITES]
  return suites


class TimeProfile(object):
  """Class for simple profiling of action, with logging of cost."""

  def __init__(self, description):
    self._description = description
    self.Start()

  def Start(self):
    self._starttime = time.time()

  def Stop(self):
    """Stop profiling and dump a log."""
    if self._starttime:
      stoptime = time.time()
      logging.info('%fsec to perform %s' %
                   (stoptime - self._starttime, self._description))
      self._starttime = None

class Xvfb(object):
  """Class to start and stop Xvfb if relevant.  Nop if not Linux."""

  def __init__(self):
    self._pid = 0

  def _IsLinux(self):
    """Return True if on Linux; else False."""
    return sys.platform.startswith('linux')

  def Start(self):
    """Start Xvfb and set an appropriate DISPLAY environment.  Linux only.

    Copied from tools/code_coverage/coverage_posix.py
    """
    if not self._IsLinux():
      return
    proc = subprocess.Popen(["Xvfb", ":9", "-screen", "0", "1024x768x24",
                             "-ac"],
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    self._pid = proc.pid
    if not self._pid:
      raise Exception('Could not start Xvfb')
    os.environ['DISPLAY'] = ":9"

    # Now confirm, giving a chance for it to start if needed.
    for test in range(10):
      proc = subprocess.Popen('xdpyinfo >/dev/null', shell=True)
      pid, retcode = os.waitpid(proc.pid, 0)
      if retcode == 0:
        break
      time.sleep(0.25)
    if retcode != 0:
      raise Exception('Could not confirm Xvfb happiness')

  def Stop(self):
    """Stop Xvfb if needed.  Linux only."""
    if self._pid:
      try:
        os.kill(self._pid, signal.SIGKILL)
      except:
        pass
      del os.environ['DISPLAY']
      self._pid = 0


def RunTests(device, test_suite, gtest_filter, test_arguments, rebaseline,
             timeout, performance_test, cleanup_test_files, tool,
             log_dump_name, apk, annotate=False):
  """Runs the tests.

  Args:
    device: Device to run the tests.
    test_suite: A specific test suite to run, empty to run all.
    gtest_filter: A gtest_filter flag.
    test_arguments: Additional arguments to pass to the test binary.
    rebaseline: Whether or not to run tests in isolation and update the filter.
    timeout: Timeout for each test.
    performance_test: Whether or not performance test(s).
    cleanup_test_files: Whether or not to cleanup test files on device.
    tool: Name of the Valgrind tool.
    log_dump_name: Name of log dump file.
    apk: boolean to state if we are using the apk based test runner
    annotate: should we print buildbot-style annotations?

  Returns:
    A TestResults object.
  """
  results = []

  if test_suite:
    global _TEST_SUITES
    if (not os.path.exists(test_suite) and
        not os.path.splitext(test_suite)[1] == '.apk'):
      logging.critical('Unrecognized test suite %s, supported: %s' %
                       (test_suite, _TEST_SUITES))
      if test_suite in _TEST_SUITES:
        logging.critical('(Remember to include the path: out/Release/%s)',
                         test_suite)
      return TestResults.FromOkAndFailed([], [BaseTestResult(test_suite, '')],
                                         False, False)
    fully_qualified_test_suites = [test_suite]
  else:
    fully_qualified_test_suites = FullyQualifiedTestSuites(apk)
  debug_info_list = []
  print 'Known suites: ' + str(_TEST_SUITES)
  print 'Running these: ' + str(fully_qualified_test_suites)
  for t in fully_qualified_test_suites:
    if annotate:
      print '@@@BUILD_STEP Test suite %s@@@' % os.path.basename(t)
    test = SingleTestRunner(device, t, gtest_filter, test_arguments,
                            timeout, rebaseline, performance_test,
                            cleanup_test_files, tool, 0, not not log_dump_name)
    test.Run()

    results += [test.test_results]
    # Collect debug info.
    debug_info_list += [test.dump_debug_info]
    if rebaseline:
      test.UpdateFilter(test.test_results.failed)
    test.test_results.LogFull()
  # Zip all debug info outputs into a file named by log_dump_name.
  debug_info.GTestDebugInfo.ZipAndCleanResults(
      os.path.join(run_tests_helper.CHROME_DIR, 'out', 'Release',
          'debug_info_dumps'),
      log_dump_name, [d for d in debug_info_list if d])

  if annotate:
    if test.test_results.timed_out:
      print '@@@STEP_WARNINGS@@@'
    elif test.test_results.failed:
      print '@@@STEP_FAILURE@@@'
    elif test.test_results.overall_fail:
      print '@@@STEP_FAILURE@@@'
    else:
      print 'Step success!'  # No annotation needed

  return TestResults.FromTestResults(results)


class TestSharder(BaseTestSharder):
  """Responsible for sharding the tests on the connected devices."""

  def __init__(self, attached_devices, test_suite, gtest_filter,
               test_arguments, timeout, rebaseline, performance_test,
               cleanup_test_files, tool):
    BaseTestSharder.__init__(self, attached_devices)
    self.test_suite = test_suite
    self.test_suite_basename = os.path.basename(test_suite)
    self.gtest_filter = gtest_filter
    self.test_arguments = test_arguments
    self.timeout = timeout
    self.rebaseline = rebaseline
    self.performance_test = performance_test
    self.cleanup_test_files = cleanup_test_files
    self.tool = tool
    test = SingleTestRunner(self.attached_devices[0], test_suite, gtest_filter,
                            test_arguments, timeout, rebaseline,
                            performance_test, cleanup_test_files, tool, 0)
    all_tests = test.test_package.GetAllTests()
    if not rebaseline:
      disabled_list = test.GetDisabledTests()
      # Only includes tests that do not have any match in the disabled list.
      all_tests = filter(lambda t:
                         not any([fnmatch.fnmatch(t, disabled_pattern)
                                  for disabled_pattern in disabled_list]),
                         all_tests)
    self.tests = all_tests

  def CreateShardedTestRunner(self, device, index):
    """Creates a suite-specific test runner.

    Args:
      device: Device serial where this shard will run.
      index: Index of this device in the pool.

    Returns:
      A SingleTestRunner object.
    """
    shard_size = len(self.tests) / len(self.attached_devices)
    shard_test_list = self.tests[index * shard_size : (index + 1) * shard_size]
    test_filter = ':'.join(shard_test_list)
    return SingleTestRunner(device, self.test_suite,
                            test_filter, self.test_arguments, self.timeout,
                            self.rebaseline, self.performance_test,
                            self.cleanup_test_files, self.tool, index)

  def OnTestsCompleted(self, test_runners, test_results):
    """Notifies that we completed the tests."""
    test_results.LogFull()
    if test_results.failed and self.rebaseline:
      test_runners[0].UpdateFilter(test_results.failed)



def _RunATestSuite(options):
  """Run a single test suite.

  Helper for Dispatch() to allow stop/restart of the emulator across
  test bundles.  If using the emulator, we start it on entry and stop
  it on exit.

  Args:
    options: options for running the tests.

  Returns:
    0 if successful, number of failing tests otherwise.
  """
  attached_devices = []
  buildbot_emulators = []

  if options.use_emulator:
    for n in range(options.use_emulator):
      t = TimeProfile('Emulator launch %d' % n)
      buildbot_emulator = emulator.Emulator(options.fast_and_loose)
      buildbot_emulator.Launch(kill_all_emulators=n == 0)
      t.Stop()
      buildbot_emulators.append(buildbot_emulator)
      attached_devices.append(buildbot_emulator.device)
    # Wait for all emulators to become available.
    map(lambda buildbot_emulator:buildbot_emulator.ConfirmLaunch(),
        buildbot_emulators)
  else:
    attached_devices = android_commands.GetAttachedDevices()

  if not attached_devices:
    logging.critical('A device must be attached and online.')
    return 1

  if (len(attached_devices) > 1 and options.test_suite and
      not options.gtest_filter and not options.performance_test):
    sharder = TestSharder(attached_devices, options.test_suite,
                          options.gtest_filter, options.test_arguments,
                          options.timeout, options.rebaseline,
                          options.performance_test,
                          options.cleanup_test_files, options.tool)
    test_results = sharder.RunShardedTests()
  else:
    test_results = RunTests(attached_devices[0], options.test_suite,
                            options.gtest_filter, options.test_arguments,
                            options.rebaseline, options.timeout,
                            options.performance_test,
                            options.cleanup_test_files, options.tool,
                            options.log_dump,
                            options.apk,
                            annotate=options.annotate)

  for buildbot_emulator in buildbot_emulators:
    buildbot_emulator.Shutdown()

  # Another chance if we timed out?  At this point It is safe(r) to
  # run fast and loose since we just uploaded all the test data and
  # binary.
  if test_results.timed_out and options.repeat:
    logging.critical('Timed out; repeating in fast_and_loose mode.')
    options.fast_and_loose = True
    options.repeat = options.repeat - 1
    logging.critical('Repeats left: ' + str(options.repeat))
    return _RunATestSuite(options)
  return len(test_results.failed)


def Dispatch(options):
  """Dispatches the tests, sharding if possible.

  If options.use_emulator is True, all tests will be run in a new emulator
  instance.

  Args:
    options: options for running the tests.

  Returns:
    0 if successful, number of failing tests otherwise.
  """
  if options.test_suite == 'help':
    ListTestSuites()
    return 0

  if options.use_xvfb:
    xvfb = Xvfb()
    xvfb.Start()

  if options.test_suite:
    all_test_suites = [options.test_suite]
  else:
    all_test_suites = FullyQualifiedTestSuites(options.apk)
  failures = 0
  for suite in all_test_suites:
    options.test_suite = suite
    failures += _RunATestSuite(options)

  if options.use_xvfb:
    xvfb.Stop()
  if options.annotate:
    print '@@@BUILD_STEP Test Finished@@@'
  return failures


def ListTestSuites():
  """Display a list of available test suites
  """
  print 'Available test suites are:'
  for test_suite in _TEST_SUITES:
    print test_suite


def main(argv):
  option_parser = run_tests_helper.CreateTestRunnerOptionParser(None,
      default_timeout=0)
  option_parser.add_option('-s', '--suite', dest='test_suite',
                           help='Executable name of the test suite to run '
                           '(use -s help to list them)')
  option_parser.add_option('-r', dest='rebaseline',
                           help='Rebaseline and update *testsuite_disabled',
                           action='store_true',
                           default=False)
  option_parser.add_option('-f', '--gtest_filter', dest='gtest_filter',
                           help='gtest filter')
  option_parser.add_option('-a', '--test_arguments', dest='test_arguments',
                           help='Additional arguments to pass to the test')
  option_parser.add_option('-p', dest='performance_test',
                           help='Indicator of performance test',
                           action='store_true',
                           default=False)
  option_parser.add_option('-L', dest='log_dump',
                           help='file name of log dump, which will be put in'
                           'subfolder debug_info_dumps under the same directory'
                           'in where the test_suite exists.')
  option_parser.add_option('-e', '--emulator', dest='use_emulator',
                           help='Run tests in a new instance of emulator',
                           type='int',
                           default=0)
  option_parser.add_option('-x', '--xvfb', dest='use_xvfb',
                           action='store_true', default=False,
                           help='Use Xvfb around tests (ignored if not Linux)')
  option_parser.add_option('--fast', '--fast_and_loose', dest='fast_and_loose',
                           action='store_true', default=False,
                           help='Go faster (but be less stable), '
                           'for quick testing.  Example: when tracking down '
                           'tests that hang to add to the disabled list, '
                           'there is no need to redeploy the test binary '
                           'or data to the device again.  '
                           'Don\'t use on bots by default!')
  option_parser.add_option('--repeat', dest='repeat', type='int',
                           default=2,
                           help='Repeat count on test timeout')
  option_parser.add_option('--annotate', default=True,
                           help='Print buildbot-style annotate messages '
                           'for each test suite.  Default=True')
  option_parser.add_option('--apk', default=False,
                           help='Use the apk test runner '
                           '(off by default for now)')
  options, args = option_parser.parse_args(argv)
  if len(args) > 1:
    print 'Unknown argument:', args[1:]
    option_parser.print_usage()
    sys.exit(1)
  run_tests_helper.SetLogLevel(options.verbose_count)
  return Dispatch(options)


if __name__ == '__main__':
  sys.exit(main(sys.argv))
