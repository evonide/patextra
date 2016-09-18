#!/usr/bin/env python3

"""
A script that is supposed to import/integrate a patch or a directory of patches into the database. Only affected files
will be connected with created patch nodes. Further, existing patches will be updated when re-running this process.
"""
from setuptools import extension

from unidiff import PatchSet
from argparse import ArgumentParser
from octopus.server.python_shell_interface import PythonShellInterface
from octopus.shell.octopus_shell_utils import reload_dir
from octopus.importer.OctopusImporter import OctopusImporter
import re
import subprocess
import sys
import operator
import os.path
import shutil
import tempfile
import threading
import time

BASEDIR = os.path.dirname(__file__)
OCTOPUS_PYLIB = 'octopus-pylib'
OCTOPUS_PYLIB_DIR = os.path.join(BASEDIR, 'python', OCTOPUS_PYLIB)
sys.path.append(OCTOPUS_PYLIB_DIR)

# TODO: remove this path once "joern-parse" is available in the global shell scope.
JOERN_PARSE_PATH = "/home/evonide/Desktop/Masterthesis/code/joern_dev/joern-parse"
PATCH_EXTRAPOLATION_DIRECTORY = "patch-extrapolation"
PATCH_PARSED_DIRECTORY = "parsed"

# Multithreading support.
MAX_NUMBER_THREADS = 8

DESCRIPTION = """Import all security patches from a specific directory/file and integrate those into the database."""

# TODO: remove this one a better way to load steps exists.
FORCE_STEPS_LOAD = False

class JoernImporter(OctopusImporter):
    def __init__(self, projectName, importSettings):
        self.importerPluginJSON = importSettings
        self.projectName = projectName


class PatchFileImporter():
    def __init__(self, project_name, code_base_location, patch_filepath):
        self.project_name = project_name
        self.patch_filepath = patch_filepath
        self.code_base_location = code_base_location
        self.print_queue = []
        self.buffer_output = True

        self.j = PythonShellInterface()
        self.j.setDatabaseName(self.project_name)

        self.temporary_directory_object = tempfile.TemporaryDirectory()
        self.temporary_directory = self.temporary_directory_object.name

    def _init_steps(self):
        dir_path = os.path.dirname(os.path.realpath(__file__)) + "/steps"
        for dirpath, dirnames, filenames in os.walk(dir_path):
            filenames[:] = [f for f in filenames if not f.startswith('.')]
            for filename in filenames:
                _, ext = os.path.splitext(filename)
                if ext == ".groovy":
                    with open(os.path.join(dirpath, filename), 'r') as f:
                        self.j.shell_connection.run_command(f.read())

    def _init_db(self):

        self.j.connectToDatabase()
        # TODO: replace this ugly code once a nicer way to load steps is available.
        if FORCE_STEPS_LOAD:
            self._init_steps()
            return
        # TODO: very ugly way of testing if the *.groovy files have already been loaded...
        try:
            self._query("createPatchNode")
        except:
            # Load additional groovy files
            self._init_steps()

    def disable_message_buffering(self):
        self.buffer_output = False

    def _print_indented(self, text, level=0, no_newline=0):
        """self._print_indented(the given text indented with 'level' many tabs.)

        Args:
          text: str The text to be printed.
          level: int The number of tabs to write before the text.
          no_newline: int Avoid a newline at the end.
        """
        new_message = ""
        for i in range (0, level):
            new_message += "\t"
        new_message += text
        if not no_newline:
            new_message += "\n"
        self.print_queue.append(new_message)
        if not self.buffer_output:
            self.flush_message_queue()

    def flush_message_queue(self):
        for message in self.print_queue:
            sys.stdout.write(message)
            sys.stdout.flush()
        self.print_queue = []

    def _query(self, query):
        """Emit a Gremlin query."""
        #print(query)
        # Try the query multiple times to cope with concurrency issues.
        max_retries = 6
        for i in range(0, max_retries):
            try:
                return self.j.runGremlinQuery(query)
            except Exception as e:
                if "OConcurrentModificationException" in str(e) or "IllegalStateException" in str(e):
                    #print("[!] -------- Concurrency error. Retrying... --------")
                    continue
                else:
                    print("[-] Query failed: {}".format(query))
                    self.flush_message_queue()
                    print("------------")
                    raise e
        raise Exception("[!] Couldn't catch concurrency issues. Please check!")

    def _get_patch_file_descripiton(self, patch_filepath):
        """Reads out the header of a given patch file."""
        patch_description = ""
        with open(self.patch_filepath) as f:
            lines = f.readlines()

            for line in lines:
                if line.startswith("diff "):
                    break
                patch_description += line
        patch_description = patch_description.strip()
        return patch_description

    def import_patch_file(self):
        """Import one single patch into the database.

        Importing means:
        1) Creating a new node for the patch (if it doesn't exist yet).
        2) Dry applying this patch on the current code base (to get updated line offsets).
        3) Parsing the patchfile and determining all effects.
        4) Retrieving all nodes of affected source code from the database.
        5) Showing some basic statistics about how well the patch could be integrated.

        Args:
          patch_filepath: str The patch's filepath.
        """
        # Initialize the database connection here since this method is started as one single thread.
        self._init_db()

        self._print_indented("[~] Importing: " + self.patch_filepath + " - ", 0, 1)

        # Create a node for this patch if it doesn't already exist.
        patch_description = self._get_patch_file_descripiton(self.patch_filepath)
        if "$/" in patch_description or "/$" in patch_description:
            raise Exception("Invalid content detected in patch description. Please adjust the code to support this.")
        patch_node_id = self._query("createPatchNode('{}', $/{}/$)".format(self.patch_filepath, patch_description))[0]

        if not patch_node_id:
            raise Exception("[!] Can't create a new node.")
        if patch_node_id == "-1":
            raise Exception("[!] A node for this patch existed more than once. Removed all instances. Please retry.")
        self._print_indented("created/resolved (" + str(patch_node_id) + ")")

        # Remove any previous operation and file nodes of this patch-node.
        self._query("cleanupPatchEffects('" + str(patch_node_id) + "')")

        # Parse the patch and read general information.
        patches = PatchSet.from_filename(self.patch_filepath, encoding='utf-8')
        number_of_patches = len(patches)
        self._print_indented("[~] Patchfile consists of {} patch/es.".format(number_of_patches))

        is_reversed_patch = False
        import_vulnerable_code = False

        # Check if a corresponding directory exists in the same directory.
        patch_directory = os.path.dirname(self.patch_filepath)
        patch_name = os.path.basename(self.patch_filepath)
        patch_raw_name = os.path.splitext(patch_name)[0]
        patch_vulnerable_code = patch_directory + "/" + patch_raw_name
        if os.path.isdir(patch_vulnerable_code):
            # Since vulnerable code already exists we can run a dry patch application on this code.
            self._copy_affected_files(patches, patch_vulnerable_code)
            fuzzed_line_offsets = self._apply_patch(self.patch_filepath, False, True)
            self._print_indented("[!] Patch was not applied as Vulnerable code is already provided for this patch.")
            import_vulnerable_code = True
        else:
            # Iterate over all affected files and copy them into a temporary location.
            # Copy all affected files into the temporary directory.
            self._copy_affected_files(patches)

            # Apply this patch to the temporary location.
            # Fetch adjusted/fuzzed lines in case the patch doesn't match perfectly.
            fuzzed_line_offsets = self._apply_patch(self.patch_filepath)
            if fuzzed_line_offsets == -1:
                # This seems to be a patch that should be applied in reverse.
                self._copy_affected_files(patches)
                is_reversed_patch = True
                fuzzed_line_offsets = self._apply_patch(self.patch_filepath, True)
                self._print_indented("[!] This patch was already applied to the project. Treating as reversed patch.")

        #print(temp_dir)
        #time.sleep(30)

        # Store some meta information about this patch.
        self._query("g.v('{}').reversed = \"{}\"".format(patch_node_id, is_reversed_patch))
        self._query("g.v('{}').originalFilesAffected = {}".format(patch_node_id, number_of_patches))

        # TODO: merge this loop and the one below...
        patch_original_lines_added = 0
        patch_original_lines_removed = 0
        patch_original_hunks = 0
        for patch in patches:
            patch_original_lines_added += patch.added
            patch_original_lines_removed += patch.removed
            patch_original_hunks += len(patch)
        # TODO: remove static string "linesAdded" here...
        self._query("g.v('{}').originalLinesAdded = {}".format(patch_node_id, patch_original_lines_added))
        self._query("g.v('{}').originalLinesRemoved = {}".format(patch_node_id, patch_original_lines_removed))
        self._query("g.v('{}').originalHunks = {}".format(patch_node_id, patch_original_hunks))

        amount_hunks_successful = 0
        amount_patchfile_connected_nodes = 0
        amount_patchfile_added_nodes = 0
        patch_i = -1
        # Each patch refers to effects on one single file.
        for patch in patches:
            patch_i += 1

            filepath = self.code_base_location + "/" + patch.path
            number_of_hunks = len(patch)
            self._print_indented("[->] " + filepath + " (" + str(number_of_hunks) + " hunk/s)", 1, 1)

            file_node_id = -1
            if import_vulnerable_code:
                extension = os.path.splitext(filepath)[1]
                # TODO: very ugly workaround. We need a way to check if the current file really contains code.
                if extension in [".c", ".cpp", ".h"]:
                    # We don't need to search for the file inside the database.
                    self._print_indented(" - importing from local file")
                else:
                    self._print_indented(" - invalid extension: {}, skipping".format(extension))
                    continue
            else:
                # TODO: I have no idea why we need to do this. OrientDB is giving us a hard time with indices :(.
                filepath = filepath[-100:]
                results = self._query("queryFileByPath('{}', true).toList().id".format(filepath))
                # TODO: remove this == [''] once the OrientDB Gremlin "List instead element" return mystery is resolved.

                # Check if this file exists in the code base (only if not provided by extracted repository content).
                if len(results) == 0 or results == ['']:
                    self._print_indented(" skipped (not found)")
                    continue
                elif len(results) > 1:
                    raise Exception("The file: " + filepath + " exists more than once in the database.")
                file_node_id = results[0]
                self._print_indented(" - resolved ({})".format(file_node_id))

            # Create a node for the affected file and connect the patch-node with it.
            patch_file_node_id = self._query("g.addVertex().id")[0]
            # Connect the patch with this newly created patch file node.
            self._query("g.addEdge(g.v('{}'), g.v('{}'), 'affects'); g.commit();".format(patch_node_id,
                                                                                         patch_file_node_id))


            # Process all hunks contained in the current patch.
            patch_hunks = self._process_patch_hunks(patch, fuzzed_line_offsets, patch_i, is_reversed_patch)
            hunks_successful = len(patch_hunks)

            self._print_indented("[!] Effects:", 2)
            self._print_indented(str(patch_hunks), 3)

            if import_vulnerable_code:
                vulnerable_file_node_id = self._import_patched_file(self.patch_filepath, patch_file_node_id, patch)
                # TODO: Add support for any effects regarding content being added i.e. handling with patched files.
                patched_file_node_id = None
                file_node_id = vulnerable_file_node_id
            elif is_reversed_patch:
                vulnerable_file_node_id = self._import_patched_file(self.patch_filepath, patch_file_node_id, patch)
                patched_file_node_id = file_node_id
            else:
                vulnerable_file_node_id = file_node_id
                patched_file_node_id = None
                self._print_indented("[~] Skipping patched (vulnerable) code import for non-reversed patch.", 2)

            # Connect the patch file node with the corresponding affected file node.
            self._query("g.addEdge(g.v('{}'), g.v('{}'), 'isFile'); g.commit();".format(patch_file_node_id,
                                                                                        file_node_id))

            # Connect the node with all code parts that it affects in the database.
            (amount_connected_nodes, amount_supposed_added_nodes) = \
                self._connect_patch(patch_file_node_id, patch_hunks, vulnerable_file_node_id, patched_file_node_id)

            amount_patchfile_connected_nodes += amount_connected_nodes
            amount_patchfile_added_nodes += amount_supposed_added_nodes
            total_effects = amount_connected_nodes + amount_supposed_added_nodes
            amount_hunks_successful += hunks_successful

            if total_effects > 0:
                self._print_indented("[+] Connected patch node with {} CPG node/s (with {} applied hunks).".format(
                    amount_connected_nodes, hunks_successful), 2)
            else:
                self._print_indented("[-] Patch can't be connected to any CPG nodes of the current code base.", 2)
                # Remove patch file node again.
                self._query("g.v('{}').remove(); g.commit();".format(patch_file_node_id))

        number_of_total_effects = amount_patchfile_connected_nodes + amount_patchfile_added_nodes
        if number_of_total_effects > 0:
            self._print_indented("[+] Patchnode was connected to {} CPG node/s (supposed total {} nodes).".format(
                    amount_patchfile_connected_nodes, number_of_total_effects))

            # Save some statistics about this patch.
            self._query("pn = g.v('{}'); pn.actualFilesAffected = pn.out.toList().size".format(patch_node_id))
            self._query("g.v('{}').actualLinesAdded = {}".format(patch_node_id, amount_patchfile_added_nodes))
            self._query("g.v('{}').actualLinesRemoved = {}".format(patch_node_id, amount_patchfile_connected_nodes))
            self._query("g.v('{}').actualHunks = {}".format(patch_node_id, amount_hunks_successful))

            # Compute the average patch hunk complexity by dividing all effects by the number of hunks.
            original_total_effects = patch_original_lines_added + patch_original_lines_removed
            average_patch_hunk_complexity = round(original_total_effects / patch_original_hunks, 3)
            self._print_indented(
                "[!] Average original patch hunk complexity is: {} (#total_effects: {} / #hunks_contained: {})".format(
                    average_patch_hunk_complexity, original_total_effects, patch_original_hunks))
            # TODO: we might want to use the actual average patch hunk complexity instead.
            #average_patch_hunk_complexity = round(number_of_total_effects / amount_hunks_successful, 3)
            #self._print_indented(
            #    "[!] Average patch hunk complexity is: {} (#total_effects: {} / #hunks_contained: {})".format(
            #        average_patch_hunk_complexity, number_of_total_effects, amount_hunks_successful))
            self._query("g.v('{}').avgHunkComplexity = {}".format(patch_node_id, average_patch_hunk_complexity))
        else:
            self._print_indented("[-] Patchfile can't be connected to the current database (no effects).")
            self._query("g.v('{}').actualFilesAffected = 0".format(patch_node_id))
            self._query("g.v('{}').actualLinesAdded = 0".format(patch_node_id))
            self._query("g.v('{}').actualLinesRemoved = 0".format(patch_node_id))
            self._query("g.v('{}').actualHunks = 0".format(patch_node_id))
            # Remove patch node again.
            # TODO: later we should remove this node again...
            #self._query("g.v('{}').remove(); g.commit();".format(patch_node_id))
        self._print_indented("------------------------------------------------------------")
        self.flush_message_queue()

    def _joern_import_file(self, patch_filename, source_filepath):
        source_directory = os.path.dirname(source_filepath)

        extrapolation_directory = self.temporary_directory + "/" + PATCH_EXTRAPOLATION_DIRECTORY
        parsed_directory = self.temporary_directory + "/" + PATCH_PARSED_DIRECTORY

        patched_file_directory = patch_filename + "/" + source_directory
        database_patchfile_filepath = patched_file_directory + "/" + os.path.basename(source_filepath)

        # Flush any files from previous runs.
        shutil.rmtree(extrapolation_directory, True)
        shutil.rmtree(parsed_directory, True)

        # Create a patch extrapolation directory.
        os.makedirs(extrapolation_directory + "/" + patched_file_directory)

        # Move our patched file into this directory.
        shutil.move(self.temporary_directory + "/" + source_filepath,
                    extrapolation_directory + "/" + patched_file_directory)

        # Invoke joern-parse to create a CSV structure in the parsed directory.
        self._print_indented("[~] Parsing patched-file: " + database_patchfile_filepath, 2)
        call_arguments = [JOERN_PARSE_PATH, PATCH_EXTRAPOLATION_DIRECTORY + "/" + patched_file_directory]
        subprocess.call(call_arguments, stdout=open(os.devnull, 'wb'), cwd=self.temporary_directory)

        #print(os.path.abspath(PATCH_PARSED_DIRECTORY))
        #time.sleep(60)

        # TODO: very dirty way of inserting the CSV directory here. We need better support in OctopusImporter.py...
        import_settings = """{
        "plugin": "importer.jar",
        "class": "joern.plugins.importer.JoernImporter",
        "settings": {
        "projectName": "%s",
        "importCSVDirectory": "%s"
        }}
        """ % ("%s", parsed_directory)

        self._print_indented("[~] Importing file into the database.", 2)
        importer = JoernImporter(self.project_name, import_settings)
        importer.executeImporterPlugin()

    def _apply_patch(self, patch_filepath, apply_reversed=False, dry_run=False):
        """Call the Linux "patch" utility on a patchfile to apply a patch and to get correct (fuzzed) line starts for
        all included (sub)patches.

        The current files the patch is supposed to be applied on might have changed over time.
        Hence, we need to tolerate some misalignments regarding line offsets. We let the "patch" utility apply a
        dry-run with our patchfile s.t. we get appropriate fuzzed line offsets (if applicable at all).

        Args:
          patch_filepath: str The patchfile we want to test against our currently existing code base.
          apply_reversed: bool True if the patch is supposed to be applied in reverse.
          dry_run: bool True if the files are not supposed to be changed (retrieve offsets only mode).

        Returns:
            If succeeds a list of lists containing the correct start line offsets for each hunk in a patch is returned.
            Else -1 is returned.
        """
        fuzzed_offsets = []
        patch_i = -1

        patch_utility_name = "patch"
        patch_utility_parameters = ["--verbose", "--ignore-whitespace",
                                    "--strip", "1",
                                    "-r", os.devnull,
                                    "-d", self.temporary_directory,
                                    "-i", patch_filepath]
        if dry_run:
            patch_utility_parameters += ["--dry-run"]
        if apply_reversed:
            patch_utility_parameters += ["-R", "-f"]

        popen_parameters = [patch_utility_name] + patch_utility_parameters
        p = subprocess.Popen(popen_parameters, stdout=subprocess.PIPE)

        for patch_stdout_line in p.stdout:
            patch_stdout_line = patch_stdout_line.decode("utf-8")

            #self._print_indented(patch_stdout_line.strip())
            if patch_stdout_line.startswith('Reversed'):
                # This is very likely a patch that was already applied. Reverse apply it instead....
                if apply_reversed:
                    raise Exception("[!] A reversed patch is trying to be applied reversed.")
                else:
                    return -1

            if patch_stdout_line.startswith('Hunk'):
                # Attention: don't forget to include the space after #1, else #10 matches, too...
                if patch_stdout_line.startswith('Hunk #1 '):
                    # 'Hunk #1 ' indicates a new patch. Create a new entry here.
                    fuzzed_offsets.append([])
                    patch_i += 1
                if "FAILED" in patch_stdout_line:
                    # This hunk couldn't be applied to the original file :(.
                    fuzzed_offsets[patch_i].append(None)
                elif "ignored" in patch_stdout_line:
                    # This hunk was ignored. The only valid reason is a missing file.
                    continue
                    # raise Exception("[!] A patch hunk was ignored by the patch utility. This should never happen.")
                else:
                    # Hunk can be applied. Filter out the matched line.
                    matched_line_regex = re.search(r"at (\d+)", patch_stdout_line)
                    if matched_line_regex:
                        matched_line = int(matched_line_regex.groups()[0])
                        fuzzed_offsets[patch_i].append(matched_line)
        p.wait()
        return fuzzed_offsets

    def _parse_patch_hunk(self, hunk, hunk_start_line):
        """Parse one single hunk.

        Parsing means detecting where lines in our current file are removed, replaced or being added.

        Args:
          hunk: Hunk One hunk of the currently processed patch.
          hunk_start_line: int The line number where this hunk begins in our current file.

        Returns:
            A dictionary with entries for added, removed and replaced lines. Each entry contains a list with
            further lists containing information regarding hunk starts and the number of affected lines.
        """
        hunk_operations = {"adds": [], "removes": [], "replaces": []}
        last_operation = ''
        replace_mode_active = 0
        current_line_number = hunk_start_line
        #print("-------------")
        #print(current_line_number)
        for s in str(hunk).splitlines():
            # Completely ignore hunk header.
            if s.startswith('@@'):
                continue
            #print(s)
            # Lines can only be added, removed or stay untouched (context lines).
            if s.startswith('+'):
                if last_operation == '-':
                    # The previous hunk is going to be replaced.
                    last_removed_chunk = hunk_operations["removes"][-1]
                    last_removed_chunk.append(1)
                    hunk_operations["replaces"].append(last_removed_chunk)
                    del hunk_operations["removes"][-1]
                    replace_mode_active = 1
                elif last_operation == '+':
                    # The previous hunk continues. We need to increment the range properly.

                    # If we are currently replacing an old hunk increment this hunk instead.
                    if replace_mode_active == 1:
                        last_replaced_chunk = hunk_operations["replaces"][-1]
                        last_replaced_chunk[2] += 1
                    else:
                        last_added_chunk = hunk_operations["adds"][-1]
                        last_added_chunk[1] += 1
                else:
                    # New Hunk begins here.
                    hunk_operations["adds"].append([current_line_number, 1])
                last_operation = '+'
            elif s.startswith('-'):
                replace_mode_active = 0
                if last_operation == '-':
                    # The previous hunk continues. We need to increment the range properly.
                    last_added_chunk = hunk_operations["removes"][-1]
                    last_added_chunk[1] += 1
                elif last_operation == '+':
                    # It should never happen that a line is being removed after one line was added.
                    raise Exception("[!] Unexpected case while parsing diff file.")
                else:
                    # New hunk begins here.
                    hunk_operations["removes"].append([current_line_number, 1])
                last_operation = '-'
            else:
                replace_mode_active = 0
                last_operation = ''

            # Update our current line number. Ignore added lines since they are not part of our original file.
            # Attention: If this is a reverse patch we need to count the number of removed lines instead.
            #if (not is_reversed_patch and last_operation != '+') or (is_reversed_patch and last_operation != '-'):
            if last_operation != '+':
                current_line_number += 1
        return hunk_operations

    def _process_patch_hunks(self, patch, fuzzed_line_offsets, patch_i, is_reversed_patch=False):
        """Determine the effects of all hunks included in one single patch.

        This method merges the fuzzed line start results with all parsed information contained in those patch hunks.

        Args:
          patch: Patch One single part of the patch file. It consists of hunks.
          fuzzed_line_offsets: [[int]] Contains the fuzzed line starts for each hunk.
          patch_i: int Index of the currently processed patch (needed for merging purposes).

        Returns:
            A list of dictionaries containing information about by a hunk added, removed and replaced lines.
        """
        hunk_i = 0
        patch_hunks = []

        # Return immediately in case this patch has no effect on the current code base.
        if len(fuzzed_line_offsets[patch_i]) == 0:
            return patch_hunks

        global_line_delta = 0
        for hunk in patch:
            # Process only if there exists a patch entry in fuzzed_offsets.
            if len(fuzzed_line_offsets[patch_i]) >= hunk_i+1:
                #self._print_indented("Hunk detected:")
                fuzzed_hunk_start_line = fuzzed_line_offsets[patch_i][hunk_i]
                if fuzzed_hunk_start_line:
                    #if hunk.source_start != fuzzed_hunk_start_line:
                    #    self._print_indented("Sourcecode seems to have changed a bit...")
                    #self._print_indented("Current LINE START:")
                    #self._print_indented(fuzzed_hunk_start_line)
                    #self._print_indented("RANGE:")
                    #self._print_indented(hunk.source_length)

                    # Adjust fuzzed_hunk_start_line to consider
                    # the delta of all (successfully) deleted and added lines so far.
                    fuzzed_hunk_start_line -= global_line_delta

                    hunk_operations = self._parse_patch_hunk(hunk, fuzzed_hunk_start_line)
                    # TODO: refactor static string here...
                    hunk_operations['metainfo'] = {}
                    hunk_operations['metainfo']['linesAdded'] = hunk.added
                    hunk_operations['metainfo']['linesRemoved'] = hunk.removed

                    # Append all hunk operations to the list of all patch hunks.
                    patch_hunks.append(hunk_operations)

                    # Retrieve the delta of this chunk and add it to the global delta so far.
                    hunk_delta = hunk.added-hunk.removed
                    # Attention: We can always ignore the global delta for reversed patches as the results we get
                    #            from fuzzy-patching (patch utlity) already point to the correct patched places.
                    # TODO: We need to add global_line_delta to "removes" operations for reversed patches...
                    #       This is necessary, as the fuzzy-patching don't include deltas for the un-patched file.
                    #       If we don't then the offset will be slightly off. That's irrelevant for now, however.
                    if not is_reversed_patch:
                        global_line_delta += hunk_delta
                else:
                    # This hunk couldn't be applied to the current file.
                    self._print_indented(
                        "[-] Patch " + str(patch_i+1) + ", Hunk " + str(hunk_i+1) + " couldn't be applied.", 2)
            else:
                raise Exception("[!] Mismatch of linux patch utility and unidiff Python library results detected.")
            hunk_i += 1
        return patch_hunks

    def _copy_affected_files(self, patches, code_base_location=None):
        """Copy all by a patch file affected files into a temporary location.

         This method merges the fuzzed line start results with all parsed information contained in this patch hunks.

         Args:
           patches: [Patch] A list of all patches contained in a patch file.
         """
        use_code_base_location = self.code_base_location
        if code_base_location:
            use_code_base_location = code_base_location

        # Copy all affected files into a temporary location.
        for patch in patches:
            original_filepath = use_code_base_location + "/" + patch.path
            # Skip non-existant files.
            if not os.path.isfile(original_filepath):
                continue
            # Create the according subdirectories in our temporary location.
            os.makedirs(self.temporary_directory + "/" + os.path.dirname(patch.path), exist_ok=True)
            # Copy the original file into our temporary location.
            temporary_filecopy_path = self.temporary_directory + "/" + patch.path
            shutil.copyfile(original_filepath, temporary_filecopy_path)


    def _import_patched_file(self, patch_filepath, patch_file_node_id, patch):
        """Import a patched file into the database.

         Args:
           patch_filepath: String The path of the original patch.
           patch_file_node_id: String The id of the patch file node.
           patch: Patch Currently applied patch.

         Returns:
            The id of the patched file node.
         """
        # Parse the patched file and import it into the database.
        patch_filename = os.path.basename(patch_filepath)
        database_patchfile_filepath = PATCH_EXTRAPOLATION_DIRECTORY + "/" + patch_filename + "/" + patch.path
        new_file_node_id = self._query("queryFileByPath('{}', false).toList().id".format(
            database_patchfile_filepath))[0]

        if new_file_node_id:
            self._print_indented("[~] Using cached patched file (" + str(new_file_node_id) + ")", 2)
            # Uncomment the lines below to remove any cached content.
            # Remove all previously stored patched files in the database.
            #self._query("g.v('" + str(new_file_node_id) + "').out.dedup().loop(2){true}{true}.remove()")
            #self._query("g.v('" + str(new_file_node_id) + "').remove()")
        else:
            self._joern_import_file(patch_filename, patch.path)
            # TODO: remove ugly workaround here (see other calls of queryFileByPath)...
            #database_patchfile_filepath = database_patchfile_filepath[-100:]
            new_file_node_id = self._query("queryFileByPath('{}', false).toList().id".format(
                database_patchfile_filepath))[0]
            self._print_indented("[~] Resolved new patched file id: " + str(new_file_node_id), 2)

        # Connect the file node with this patched file node.
        self._query("g.addEdge(g.v('{}'), g.v('{}'), 'resultsIn')".format(
            patch_file_node_id, new_file_node_id))
        return new_file_node_id

    def _connect_patch(self, patch_file_node_id, patch_hunks, vulnerable_file_node_id, patched_file_node_id=None):
        """Connect all affected source code nodes with corresponding hunk nodes.

        We require at least one graph database entry for a vulnerable file representation. This representation is then
        used to connect all "remove" operations. Accordingly, if a patched file version is available, we will connect
        all "adds" operations, too. This also holds for "replaces" operations where removed content is connected with
        the vulnerable file and added content with the patched file (if available).

       Args:
         patch_file_node_id: str A patch file node id i.e. the node representing all effects regarding one file.
         patch_hunks: [{}] A list of hunk operations.
         vulnerable_file_node_id: str The id of the corresponding unpatched/vulnerable file version in the database.
         patched_file_node_id: str The id of the corresponding patched file version in the database.
       """
        amount_connected_nodes = 0
        amount_supposed_added_nodes = 0
        for hunk_operations in patch_hunks:
            # Create a node for the affected file and connect the patch-node with it.
            new_hunk_node_id = self._query("g.addVertex().id")[0]
            amount_hunk_connected_nodes = 0
            amount_hunk_supposed_added_nodes = 0

            for operation in hunk_operations:
                # TODO: remove static string here.
                # TODO: store metainfos at another place than here.
                if operation == 'metainfo':
                    metainfos = hunk_operations[operation]
                    # Store some infos about this patch and continue...
                    for metainfo in metainfos:
                        self._query("g.v('{}').{} = {}".format(new_hunk_node_id, metainfo, metainfos[metainfo]))
                    continue
                affected_hunk_segments = hunk_operations[operation]

                for affected_segment in affected_hunk_segments:
                    line_number_start = affected_segment[0]
                    lines_affected = affected_segment[1]
                    lines_replacement = 0
                    if operation == 'replaces':
                        lines_replacement = affected_segment[2]

                    # TODO: For now we don't support content that is being added by patches. However, we are considering
                    #       how many lines would be added for statistics purposes.
                    if operation == 'adds':
                        # Here we would need to check if patched_file_node_id set at all.
                        amount_hunk_supposed_added_nodes += lines_affected
                        continue
                    elif operation == 'replaces':
                        amount_hunk_supposed_added_nodes += lines_replacement

                    source_start = line_number_start
                    source_end = source_start + lines_affected - 1

                    # Connect all code nodes in a specific range with the patch file node.
                    query_connect_patch = "connectPatchWithAffectedCode('{}', '{}', '{}', '{}', {}, {})".format(
                        patch_file_node_id, vulnerable_file_node_id, new_hunk_node_id,
                        operation, source_start, source_end)
                    number_of_affected_nodes = int(self._query(query_connect_patch)[0])

                    amount_hunk_connected_nodes += number_of_affected_nodes

            # Connect the patch file node with the hunk node.
            self._query("g.v('{}').addEdge('applies', g.v('{}'))".format(patch_file_node_id, new_hunk_node_id))
            # Add the number of connected hunk effects to the number of all connected nodes.
            amount_connected_nodes += amount_hunk_connected_nodes
            # Add the number of all nodes that were supposed to be added.
            amount_supposed_added_nodes += amount_hunk_supposed_added_nodes

        #if amount_supposed_added_nodes > 0:
        #    print("I would add {} nodes. However, not fully supported atm.".format(amount_supposed_added_nodes))

        return (amount_connected_nodes, amount_supposed_added_nodes)



class PatchImporter():
    def __init__(self, databaseName = 'octopusDB'):
        self.argParser = ArgumentParser(description = DESCRIPTION)
        self.argParser.add_argument('project')
        # TODO: For now we are ignoring the databaseName in the init parameter. This should be adjusted.
        self.argParser.add_argument(
                "directory",
                help = """The directory containing *.patch files.""")

        self.args = self.argParser.parse_args()
        self.project_name = self.args.project

        self.j = PythonShellInterface()
        self.j.setDatabaseName(self.project_name)
        self.j.connectToDatabase()
        # TODO: replace this ugly code once a nicer way to load steps is available.
        old_stdout = sys.stdout
        sys.stdout = open(os.devnull, "w")
        dir_path = os.path.dirname(os.path.realpath(__file__))
        reload_dir(self.j.shell_connection, dir_path + "/steps")
        sys.stdout.close()
        sys.stdout = old_stdout
        # ------------------------------------------------------------------------

        # Get the current code base location.
        # TODO: Is there no cleaner way to get the root element of a graph?
        #self.code_base_location = self._query("queryNodeIndex('key:1').filepath")[0]
        self.code_base_location = self._query("execSQLQuery('SELECT FROM #9:1').filepath")[0]

        if not self.code_base_location:
            raise Exception("[!] Couldn't retrieve the code base location.")

        self._print_indented("[~] Using code base location: " + self.code_base_location)
        # An array storing all running importer threads.
        self.import_threads = []
        # Ensure that the patch index was built.
        self._query("createPatchIndex()")

    def _query(self, query):
        """Emit a Gremlin query."""
        return self.j.runGremlinQuery(query)

    def _print_indented(self, text, level=0, no_newline=0):
        """self._print_indented(the given text indented with 'level' many tabs.)

        Args:
          text: str The text to be printed.
          level: int The number of tabs to write before the text.
          no_newline: int Avoid a newline at the end.
        """
        for i in range (0, level):
            sys.stdout.write("\t")
        if no_newline:
            sys.stdout.write(text)
        else:
            print(text)

    def start_new_import_thread(self, patch_path):
        """Starts a new importer thread once there is a free slot.

        Waits until less than MAX_NUMBER_THREADS threads are running and starts a new importer thread.

        Args:
        patch_path: String The patch filepath.
        """
        patchFileImporter = PatchFileImporter(self.project_name, self.code_base_location, patch_path)

        scheduled = False
        while not scheduled:
            # Remove any threads that have finished by now.
            #self.import_threads = [thread for thread in self.import_threads if thread.isAlive()]
            running_threads = [thread for thread in self.import_threads if thread.isAlive()]
            number_running_threads = len(running_threads)
            # Start threads only if there are free slots left.
            if number_running_threads < MAX_NUMBER_THREADS:
                new_thread = threading.Thread(target=patchFileImporter.import_patch_file)
                self.import_threads.append(new_thread)
                new_thread.start()
                self._print_indented("[~] Started new thread - running {}/{}.".format(
                    (number_running_threads + 1), MAX_NUMBER_THREADS))
                scheduled = True
            # Wait some milliseconds until retry.
            time.sleep(0.05)

    def import_directory(self):
        """Import all patches contained in a given directory.

        See the PatchFileImporter class for more information regarding importing.
        """
        # We need to resolve the absolute path to avoid multiple entries for the same patch in the database.
        import_path = os.path.abspath(self.args.directory)

        if os.path.isfile(import_path):
            # Include a single patch file.
            patchFileImporter = PatchFileImporter(self.project_name, self.code_base_location, import_path)
            patchFileImporter.disable_message_buffering()
            patchFileImporter.import_patch_file()
        else:
            self._print_indented("[~] Importing directory: " + import_path)
            self._print_indented("------------------------------------------------------------")
            # Scan directory for patch files to import and start threaded importing.
            # Order all patches by size.
            patch_files = [[file, os.path.getsize(import_path + "/" + file)]
                           for file in os.listdir(import_path)
                           if file.endswith(".patch")]
            patches_sorted_by_size = sorted(patch_files, key=operator.itemgetter(1))
            # Iterate over the smallest patches first.
            for (file,size) in patches_sorted_by_size:
                patch_path = import_path + '/' + file
                self._print_indented("[~] Starting thread for: " + patch_path)
                self.start_new_import_thread(patch_path)
                sys.stdout.flush()
            # Wait until all remaining threads have terminated.
            for thread in self.import_threads:
                thread.join()

        self._print_indented("[+] Importing finished.")

if __name__ == '__main__':
    tool = PatchImporter()
    tool.import_directory()
