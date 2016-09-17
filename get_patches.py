#!/usr/bin/env python3

"""
Small utility to retrieve all patches currently available in the database.
"""
from argparse import ArgumentParser
from octopus.server.python_shell_interface import PythonShellInterface
from octopus.shell.octopus_shell_utils import reload_dir
import sys
import os


DESCRIPTION = """Retrieve all security patches from the database."""

if __name__ == '__main__':
    argParser = ArgumentParser(description = DESCRIPTION)
    argParser.add_argument('project')
    args = argParser.parse_args()
    projectName = args.project

    # Setup
    dbConnection = PythonShellInterface()
    dbConnection.setDatabaseName(projectName)
    dbConnection.connectToDatabase()

    # Load additional groovy files (mute any output in the meantime).
    old_stdout = sys.stdout
    sys.stdout = open(os.devnull, "w")
    dir_path = os.path.dirname(os.path.realpath(__file__)) + "/steps"
    reload_dir(dbConnection.shell_connection, dir_path)
    sys.stdout.close()
    sys.stdout = old_stdout

    show_fields = ['id', 'filepath', 'avgHunkComplexity', 'affected_functions', 'is_reversed', 'originalHunks']

    # Get the current code base location.
    for field in show_fields:
        sys.stdout.write(field + "\t")
    print("")
    patch_nodes_results = dbConnection.runGremlinQuery('get_all_patches()')
    for patch_node_result in patch_nodes_results:
        patch_infos = patch_node_result.split("\t")
        for info in patch_infos:
            sys.stdout.write(info + "\t")
        print("")

