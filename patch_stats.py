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

    print("[~] Imported patch statistics for project: {}".format(projectName))
    imported_patches = int(dbConnection.runGremlinQuery('queryPatchIndex("/.*patch/").toList().size')[0])
    print("[~] Currently imported patches: {}".format(imported_patches))
    active_patches = int(dbConnection.runGremlinQuery(
        'queryPatchIndex("/.*patch/").filter{it.out("affects").toList().size > 0}.toList().size')[0])
    print("[~] Currently active patches: {}".format(active_patches))
    print("[~] Ratio: " + str(round(active_patches/imported_patches, 2)))
    show_fields  = ['actualFilesAffected', 'originalFilesAffected']
    show_fields += ['actualLinesAdded', 'originalLinesAdded']
    show_fields += ['actualLinesRemoved', 'originalLinesRemoved']
    show_fields += ['actualHunks', 'originalHunks']

    sys.stdout.write("projectName \& ")
    for field in show_fields:
        sys.stdout.write(field + " \& ")
    print("")

    sys.stdout.write(projectName + " \& ")
    sum_results = []
    for field in show_fields:
        sum = int(dbConnection.runGremlinQuery('queryPatchIndex("/.*patch/").has("{}").{}.sum()'.format(field, field))[0])
        sys.stdout.write(str(sum) + " \& ")
        sum_results.append(sum)
    print("\n")
    print("Relative amounts:")
    i = 0
    for field in show_fields:
        if (i+1) % 2 == 0:
            sys.stdout.write(field + " ")
        else:
            sys.stdout.write(field + "/")
        i += 1
    print("")
    i = 0
    for field in show_fields:
        if (i+1) % 2 == 0:
            sys.stdout.write(str(round(sum_results[i-1]/sum_results[i], 2)) + " \& ")
        i += 1
    print("\n")
