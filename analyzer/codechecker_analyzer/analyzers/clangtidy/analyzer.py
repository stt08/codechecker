# -------------------------------------------------------------------------
#                     The CodeChecker Infrastructure
#   This file is distributed under the University of Illinois Open Source
#   License. See LICENSE.TXT for details.
# -------------------------------------------------------------------------
"""
"""
from __future__ import print_function
from __future__ import division
from __future__ import absolute_import

import os
import re
import shlex
import subprocess

from codechecker_common.logger import get_logger

from codechecker_analyzer import host_check
from codechecker_analyzer import env

from .. import analyzer_base
from ..flag import has_flag
from ..clangsa.analyzer import ClangSA

from . import config_handler
from . import result_handler

LOG = get_logger('analyzer')


def parse_checkers(tidy_output):
    """
    Parse clang tidy checkers list.
    Skip clang static analyzer checkers.
    Store them to checkers.
    """
    checkers = []
    pattern = re.compile(r'^\S+$')
    for line in tidy_output.splitlines():
        line = line.strip()
        if line.startswith('Enabled checks:') or line == '':
            continue
        elif line.startswith('clang-analyzer-'):
            continue
        match = pattern.match(line)
        if match:
            checkers.append((match.group(0), ''))
    return checkers


class ClangTidy(analyzer_base.SourceAnalyzer):
    """
    Constructs the clang tidy analyzer commands.
    """

    ANALYZER_NAME = 'clang-tidy'

    def add_checker_config(self, checker_cfg):
        LOG.error("Not implemented yet")

    @classmethod
    def get_analyzer_checkers(cls, cfg_handler, environ):
        """
        Return the list of the supported checkers.
        """
        analyzer_binary = cfg_handler.analyzer_binary

        command = [analyzer_binary, "-list-checks", "-checks='*'"]

        try:
            command = shlex.split(' '.join(command))
            result = subprocess.check_output(command, env=environ,
                                             universal_newlines=True)
            return parse_checkers(result)
        except (subprocess.CalledProcessError, OSError):
            return []

    def construct_analyzer_cmd(self, result_handler):
        """
        """
        try:
            config = self.config_handler

            analyzer_cmd = [config.analyzer_binary]

            # Disable all checkers except compiler warnings by default.
            # The latest clang-tidy (3.9) release enables clang static analyzer
            # checkers by default. They must be disabled explicitly.
            # For clang compiler warnings a correspoding
            # clang-diagnostic error is generated by Clang tidy.
            # They can be disabled by this glob -clang-diagnostic-*
            checkers_cmdline = '-*,-clang-analyzer-*,clang-diagnostic-*'

            compiler_warnings = []

            # Config handler stores which checkers are enabled or disabled.
            for checker_name, value in config.checks().items():
                enabled, _ = value

                # Checker name is a compiler warning.
                if checker_name.startswith('W'):
                    warning_name = checker_name[4:] if \
                        checker_name.startswith('Wno-') else checker_name[1:]

                    if enabled:
                        compiler_warnings.append('-W' + warning_name)
                    else:
                        compiler_warnings.append('-Wno-' + warning_name)

                    continue

                if enabled:
                    checkers_cmdline += ',' + checker_name
                else:
                    checkers_cmdline += ',-' + checker_name

            analyzer_cmd.append("-checks='%s'" % checkers_cmdline.lstrip(','))

            analyzer_cmd.extend(config.analyzer_extra_arguments)

            if config.checker_config:
                analyzer_cmd.append('-config="' + config.checker_config + '"')

            analyzer_cmd.append(self.source_file)

            analyzer_cmd.append("--")

            analyzer_cmd.append('-Qunused-arguments')

            # Enable these compiler warnings by default.
            analyzer_cmd.extend(['-Wall', '-Wextra'])

            compile_lang = self.buildaction.lang

            if not has_flag('-x', analyzer_cmd):
                analyzer_cmd.extend(['-x', compile_lang])

            if not has_flag('--target', analyzer_cmd) and \
                    self.buildaction.target.get(compile_lang, "") != "":
                analyzer_cmd.append(
                    "--target=" + self.buildaction.target.get(compile_lang,
                                                              ""))

            analyzer_cmd.extend(self.buildaction.analyzer_options)

            env.extend_analyzer_cmd_with_resource_dir(
                analyzer_cmd, config.compiler_resource_dir)

            analyzer_cmd.extend(
                self.buildaction.compiler_includes[compile_lang])

            if not has_flag('-std', analyzer_cmd) and not \
                    has_flag('--std', analyzer_cmd):
                analyzer_cmd.append(
                    self.buildaction.compiler_standard.get(compile_lang, ""))

            analyzer_cmd.extend(compiler_warnings)

            return analyzer_cmd

        except Exception as ex:
            LOG.error(ex)
            return []

    def get_analyzer_mentioned_files(self, output):
        """
        Parse Clang-Tidy's output to generate a list of files that were
        mentioned in the standard output or standard error.
        """

        if not output:
            return set()

        # A line mentioning a file in Clang-Tidy's output looks like this:
        # /home/.../.cpp:L:C: warning: foobar.
        regex = re.compile(
            # File path followed by a ':'.
            r'^(?P<path>[\S ]+?):'
            # Line number followed by a ':'.
            r'(?P<line>\d+?):'
            # Column number followed by a ':' and a space.
            r'(?P<column>\d+?): ')

        paths = []

        for line in output.splitlines():
            match = re.match(regex, line)
            if match:
                paths.append(match.group('path'))

        return set(paths)

    @classmethod
    def resolve_missing_binary(cls, configured_binary, environ):
        """
        In case of the configured binary for the analyzer is not found in the
        PATH, this method is used to find a callable binary.
        """

        LOG.debug("%s not found in path for ClangTidy!", configured_binary)

        if os.path.isabs(configured_binary):
            # Do not autoresolve if the path is an absolute path as there
            # is nothing we could auto-resolve that way.
            return False

        # clang-tidy, clang-tidy-5.0, ...
        clangtidy = env.get_binary_in_path(['clang-tidy'],
                                           r'^clang-tidy(-\d+(\.\d+){0,2})?$',
                                           environ)

        if clangtidy:
            LOG.debug("Using '%s' for Clang-tidy!", clangtidy)
        return clangtidy

    def construct_result_handler(self, buildaction, report_output,
                                 severity_map, skiplist_handler):
        """
        See base class for docs.
        """
        report_hash = self.config_handler.report_hash
        res_handler = result_handler.ClangTidyPlistToFile(buildaction,
                                                          report_output,
                                                          report_hash)

        res_handler.severity_map = severity_map
        res_handler.skiplist_handler = skiplist_handler
        return res_handler

    @classmethod
    def construct_config_handler(cls, args, context):
        handler = config_handler.ClangTidyConfigHandler()
        handler.analyzer_binary = context.analyzer_binaries.get(
            cls.ANALYZER_NAME)

        # FIXME We cannot get the resource dir from the clang-tidy binary,
        # therefore we get a sibling clang binary which of clang-tidy.
        # TODO Support "clang-tidy -print-resource-dir" .
        check_env = env.extend(context.path_env_extra,
                               context.ld_lib_path_extra)
        # Overwrite PATH to contain only the parent of the clang binary.
        if os.path.isabs(handler.analyzer_binary):
            check_env['PATH'] = os.path.dirname(handler.analyzer_binary)
        clang_bin = ClangSA.resolve_missing_binary('clang',
                                                   check_env)
        handler.compiler_resource_dir = \
            host_check.get_resource_dir(clang_bin, context)

        try:
            with open(args.tidy_args_cfg_file, 'rb') as tidy_cfg:
                handler.analyzer_extra_arguments = \
                    re.sub(r'\$\((.*?)\)', env.replace_env_var,
                           tidy_cfg.read().strip())
                handler.analyzer_extra_arguments = \
                    shlex.split(handler.analyzer_extra_arguments)
        except IOError as ioerr:
            LOG.debug_analyzer(ioerr)
        except AttributeError as aerr:
            # No clang tidy arguments file was given in the command line.
            LOG.debug_analyzer(aerr)

        try:
            # The config file dumped by clang-tidy contains "..." at the end.
            # This has to be emitted, otherwise -config flag of clang-tidy
            # cannot consume it.
            with open(args.tidy_config, 'rb') as tidy_config:
                lines = tidy_config.readlines()
                lines = filter(lambda x: x != '...\n', lines)
                handler.checker_config = ''.join(lines)
        except IOError as ioerr:
            LOG.debug_analyzer(ioerr)
        except AttributeError as aerr:
            # No clang tidy config file was given in the command line.
            LOG.debug_analyzer(aerr)

        check_env = env.extend(context.path_env_extra,
                               context.ld_lib_path_extra)

        checkers = ClangTidy.get_analyzer_checkers(handler, check_env)

        # Read clang-tidy checkers from the config file.
        clang_tidy_checkers = context.checker_config.get(cls.ANALYZER_NAME +
                                                         '_checkers')

        try:
            cmdline_checkers = args.ordered_checkers
        except AttributeError:
            LOG.debug_analyzer('No checkers were defined in '
                               'the command line for %s',
                               cls.ANALYZER_NAME)
            cmdline_checkers = None

        handler.initialize_checkers(
            context.available_profiles,
            context.package_root,
            checkers,
            clang_tidy_checkers,
            cmdline_checkers,
            'enable_all' in args and args.enable_all)

        return handler
