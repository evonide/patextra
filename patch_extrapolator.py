#!/usr/bin/env python3

"""
This library automatically finds interesting by a patch affected nodes inside the graph database and
extrpolates them in order to find similar code segments like the ones affected by the initial patch.
"""

import os
import shutil
import subprocess
import sys

from joern.shelltool.AccTool import AccTool
from octopus.shell.octopus_shell_utils import reload_dir
from node_extrapolator import NodeExtrapolator

DESCRIPTION = """Takes a patch-node-id/part-of-patch-name and tries to automatically extrapolate it."""


class PatchExtrapolator(AccTool):

    def __init__(self, DESCRIPTION):
        AccTool.__init__(self, DESCRIPTION)
        self.argParser.add_argument(
            '--verbose',
            action='store_true',
            default=False,
            help='Verbose mode: show messages regarding the extrapolation.'
        )

        self.stepsLoaded = False

    @staticmethod
    def die(message):
        sys.stderr.write(message + "\n")
        sys.exit()

    def loadSteps(self):
        if self.stepsLoaded:
            return
        # Load additional groovy files (mute any output in the meantime).
        old_stdout = sys.stdout
        sys.stdout = open(os.devnull, "w")
        dir_path = os.path.dirname(os.path.realpath(__file__)) + "/steps"
        reload_dir(self.dbInterface.j.shell_connection, dir_path)
        sys.stdout.close()
        sys.stdout = old_stdout
        self.stepsLoaded = True

    def _print_indented(self, text, level=0, no_newline=0):
        """self._print_indented(the given text indented with 'level' many tabs.)

        Args:
          text: str The text to be printed.
          level: int The number of tabs to write before the text.
          no_newline: int Avoid a newline at the end.
        """
        if self.args.verbose:
            for i in range (0, level):
                sys.stdout.write("\t")
            if no_newline:
                sys.stdout.write(text)
            else:
                print(text)

    def processLine(self, patch_id):
        self.loadSteps()
        # TODO: Currently, extrapolation support is only provided for replaces operations.
        #       Extend it to support other operations like "removes", too.

        # Support for providing parts of the patch filename instead of its id.
        if patch_id[0] != '#':
            patch_id = self._runGremlinQuery("queryPatchIndex('/.*{}.*/').id".format(patch_id))[0]
            if not patch_id:
                self._print_indented("[-] No such patch available. Are you using the correct project?")
                exit()

        patchfile_path = self._runGremlinQuery("g.v('{}').filepath".format(patch_id))[0]
        self._print_indented("[~] Resolved patch file: {} (id: {})".format(patchfile_path, patch_id))

        # TODO: Verify patch id correctness (exists etc).

        affected_files_query = "getPatchAffectedFiles('{}')".format(patch_id)
        affected_file_ids = self._runGremlinQuery(affected_files_query)
        if affected_file_ids == ['']:
            self._print_indented("[~] No effects - skipping patch.", 1)
            self._print_indented("------------------------------------------------------------")
            return

        num_affected_files = len(affected_file_ids)
        actual_affected_files = self._runGremlinQuery("g.v('{}').numAffectedFiles".format(patch_id))[0]

        self._print_indented("[~] Currently affected number of files: {}/{}".format(
                            num_affected_files, actual_affected_files), 1)

        for affected_file_id in affected_file_ids:
            affected_filename = self._runGremlinQuery("g.v('{}').out('isFile').filepath".format(affected_file_id))[0]
            self._print_indented("[~] Affected file: {}".format(affected_filename), 1)

            callees_query = "getPatchOperations('{}')".format(affected_file_id)
            patch_callee_ids = self._runGremlinQuery(callees_query)
            if patch_callee_ids == ['']:
                self._print_indented("[~] Skipped - no callees here.", 2)
                continue
            number_of_callees = len(patch_callee_ids)
            self._print_indented("[~] # of affected callees: {}".format(number_of_callees), 2)

            operation_is_extrapolatable = (number_of_callees == 1)

            show_max_number_of_callees = 3
            i = 1
            for patch_callee_id in patch_callee_ids:
                if i > show_max_number_of_callees:
                    self._print_indented("[!] Skipping {} call/s (too many callees affected).".format(
                                        number_of_callees-show_max_number_of_callees), 2)
                    break
                callee_symbol = self._runGremlinQuery("g.v('{}').code".format(patch_callee_id))[0]
                callee_location = self._runGremlinQuery("g.v('{}')._().statements().location".format(patch_callee_id))[0]
                self._print_indented("[~] Callee: {} (located on line @{})".format(
                                    callee_symbol, callee_location), 2)
                i += 1

            # Print out callee information here.
            for patch_callee_id in patch_callee_ids:
                sys.stdout.write(patch_id + "\t")
                # Print out all callee ids here.
                sys.stdout.write(patch_callee_id + "\t")
                # Show the number of callees for this file.
                sys.stdout.write("{} callee/s".format(number_of_callees))
                print("")
        self._print_indented("------------------------------------------------------------")


if __name__ == '__main__':
    tool = PatchExtrapolator(DESCRIPTION)
    tool.run()
