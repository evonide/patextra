#!/usr/bin/env python3

"""
This library allows to find similar code segments once it is given a starting node.
"""

import os
import shutil
import subprocess
import sys

from joern.shelltool.AccTool import AccTool
from octopus.mlutils.KNN import KNN
"""
import pkgutil
import octopus
package = octopus
for importer, modname, ispkg in pkgutil.walk_packages(path=package.__path__,
                                                      prefix=package.__name__+'.',
                                                      onerror=lambda x: None):
    print(modname)
exit()
"""
DESCRIPTION = """Takes a node-id of any statement and extrapolates it to further code places."""


NUMBER_OF_NEIGHBORS_TO_DISPLAY = 40
# Number of hops to follow during slicing.
SLICING_PDG_PRECISION = 5

DEFAULT_EBMDDINGS_CACHE_DIRECTORY = ".cache"

class NodeExtrapolator(AccTool):

    def __init__(self, DESCRIPTION):
        AccTool.__init__(self, DESCRIPTION)
        self.argParser.add_argument(
            '-d',
            type=str,
            help='The directory which will contain embeddings.'
        )
        self.argParser.add_argument(
            '--force',
            action='store_true',
            default=False,
            help='overwrite existing directories'
        )

        self.argParser.add_argument(
            '--verbose',
            action='store_true',
            default=False,
            help='Verbose mode: show messages regarding the extrapolation.'
        )
        # Setup
        #self.dbInterface.addStepsDir("steps")

        # Parse command line already here to initialize further class variables.
        self._parseCommandLine()
        if not self.args.d:
            self.args.d = DEFAULT_EBMDDINGS_CACHE_DIRECTORY

        self.embeddings_directory = self.args.d
        self.embeddings_data_directory = os.path.join(self.embeddings_directory, 'data')
        self.embeddings_toc_path = os.path.join(self.embeddings_directory, 'TOC')

    def _print(self, message):
        if self.args.verbose:
            print(message)
            sys.stdout.flush()

    def _getSinkSymbol(self, node_id):
        symbol_query = "g.v('%s')._().statements().children().out.dedup().loop(2){true}{true}.filter{it.nodeType == 'Callee'}.code"
        symbol_query = symbol_query % node_id

        sink_symbol = self._runGremlinQuery(symbol_query)
        if not sink_symbol:
            sys.stderr.write("[-] Couldn't query the supplied node's code.\n")
            sys.exit()
        return sink_symbol[0]

    def _getSimilarSinkNodeIDs(self, sink_symbol):
        more_sinks_query = "getCallsTo('%s').transform{it.id}"
        more_sinks_query = more_sinks_query % sink_symbol

        sink_node_ids = self._runGremlinQuery(more_sinks_query)
        if not sink_node_ids:
            sys.stderr.write("[-] Strange error occured. At least the initial sink must exist...\n")
            sys.exit()
        return sink_node_ids

    def _resolveNodeSymbols(self, sink_slice_node_ids):
        node_ids_to_symbols = """idListToNodes(%s).astNodes()
        .filter{
            it.nodeType == 'IdentifierDeclType' || it.nodeType == 'ParameterType' || it.nodeType == 'Callee' || it.nodeType == 'Sizeof'
        }
        .code.toList()"""
        node_ids_to_symbols = node_ids_to_symbols % sink_slice_node_ids

        node_symbols = self._runGremlinQuery(node_ids_to_symbols)
        if not node_symbols:
            sys.stderr.write("[-] Couldn't resolve the supplied nodes' symbols.\n")
            sys.exit()
        return node_symbols

    def _querySlicing(self, query):
        slice_node_ids = self._runGremlinQuery(query)
        if not slice_node_ids or 'Exception' in slice_node_ids[0]:
            sys.stderr.write("[-] Couldn't slice from this sink.\n")
            sys.exit()
        return slice_node_ids

    def _sliceBackwards(self, node_id, slicing_precision=SLICING_PDG_PRECISION):
        # TODO: fix very dirty workaround with join newline style... Python API FIX required???
        slice_query = """
        g.v('%s')._().sideEffect
        {
            if(it.nodeType == 'Argument') {
                symbols = it._().uses().code.toList()
            } else {
                symbols = it._().statements().out('USE', 'DEF').code.toList()
            }
        }.statements().transform{
            it._().backwardSlice(symbols, %d).id.toList().join("\\n")
        }
        """ % (node_id, slicing_precision)
        return self._querySlicing(slice_query)

    def _sliceForwards(self, node_id, slicing_precision=SLICING_PDG_PRECISION):
        slice_query = """
        g.v('%s').sideEffect
        {
            if(it.nodeType == 'Callee') {
                symbols = it._().matchParents{it.nodeType == 'AssignmentExpression'}.lval().code.toList()
            } else if(it.nodeType == 'Argument') {
                 symbols = it._().defines().code.toList()
            } else {
                 symbols = it._().statements().out('USE', 'DEF').code.toList()
            }
        }._().statements().transform{
            it._().forwardSlice(symbols, %d).id.toList().join("\\n")
        }
        """ % (node_id, slicing_precision)
        return self._querySlicing(slice_query)

    def _sliceBidrectional(self, node_id):
        slicing_precision = SLICING_PDG_PRECISION / 2

        backwards_slice_node_ids = self._sliceBackwards(node_id, slicing_precision)
        forwards_slice_node_ids = self._sliceForwards(node_id, slicing_precision)
        bidirectional_slice_node_ids = backwards_slice_node_ids + forwards_slice_node_ids
        return bidirectional_slice_node_ids

    def _symbolsToStringEmbeddings(self, symbols):
        if os.path.exists(self.embeddings_directory):
            if self.args.force:
                self._print("[!] Removing existing directory.")
                shutil.rmtree(self.embeddings_directory)
            else:
                sys.stderr.write("[-] Please remove the embeddings directory first or supply --force.\n")
                sys.exit()
        os.makedirs(self.embeddings_data_directory)

        table_of_contents = {}
        # Iterate over all given symbols and write them into the embedding's data directory.
        for node_id in symbols:
            if node_id not in table_of_contents:
                table_of_contents[node_id] = len(table_of_contents)
            features = symbols[node_id]

            suffix = str(table_of_contents[node_id])
            datapoint_path = os.path.join(self.embeddings_data_directory, suffix)
            with open(datapoint_path, 'a') as f:
                for feature in features:
                    f.write(feature)
                    f.write(os.linesep)

        # Finalize by writing the table of contents file.
        with open(self.embeddings_toc_path, 'w') as f:
            for key, _ in sorted(table_of_contents.items(), key=lambda x: x[1]):
                key_entry = str(key)
                f.write(key_entry)
                f.write(os.linesep)

    def _findNearestNeighbors(self, initial_node_id):
        knn = KNN()
        knn.setEmbeddingDir(self.embeddings_directory)
        # Limit possible neighbours to those specified in the provided file.
        knn.setLimitArray(None)
        # Number of nearest neighbors to determine.
        knn.setK(NUMBER_OF_NEIGHBORS_TO_DISPLAY)
        # Number of dimensions for SVD (0 -> don't use SVD).
        knn.setSVDk(0)
        # Cache calculated distances on disk.
        knn.setNoCache(False)

        try:
            knn.initialize()
        except IOError:
            sys.stderr.write("[-] Can't read the embeddings directory.\n")
            sys.exit()

        try:
            neighbors = knn.getNeighborsFor(initial_node_id)
            for (n, similarity) in neighbors:
                function_name = self._runGremlinQuery("g.v('{}')._().functions().name".format(n))[0]
                #filepath = self._runGremlinQuery("g.v('{}')._().functions().functionToFile().filepath".format(n))[0]
                #+ "\t" + filepath
                location = self._runGremlinQuery("g.v('{}')._().statements().location".format(n))[0]
                location = location.split(":")[0]
                print(n + "\t" + str(similarity) + "\t" + function_name + "\t" + location)
        except KeyError:
            sys.stderr.write("[-] No data point found for %s.\n" % initial_node_id)
            sys.exit()

    def processLine(self, node_id):
        self._print("[~] Starting lookup for node: " + str(node_id))

        #self._findNearestNeighbors(node_id)
        #exit()

        # For now we will assume that this is a simple sink!
        # Retrieve the sink symbol...
        sink_symbol = self._getSinkSymbol(node_id)
        if not sink_symbol:
            sys.stderr.write("[-] Couldn't resolve code-symbol from node.\n")
            sys.exit()
        self._print("[+] Retrieved sink symbol: " + sink_symbol)

        # 1) Retrieve all interesting sink nodes
        sink_node_ids = self._getSimilarSinkNodeIDs(sink_symbol)
        #print sink_node_ids
        self._print("[+] Found " + str(len(sink_node_ids)) + " similar sinks.")
        self._print("[~] Applying program slicing to all found sinks.")

        # Add the initial node to the set of all sink nodes, too.
        sink_node_ids.insert(0, node_id)

        resolved_symbols = {}
        for sink_node_id in sink_node_ids:
            #self._print("Slicing node: " + str(sink_node_id))
            #sink_slice_node_ids = self._sliceBackwards(sink_node_id)
            sink_slice_node_ids = self._sliceForwards(sink_node_id)
            #sink_slice_node_ids = self._sliceBidrectional(sink_node_id)

            code = self._resolveNodeSymbols(sink_slice_node_ids)
            resolved_symbols[sink_node_id] = code


        # Store all resolved symbols
        self._symbolsToStringEmbeddings(resolved_symbols)
        self._print("[+] Stored all string literals in the embeddings directory.")
        self._print("[~] Invoking sally to create proper features and a libsvm file.")


        # TODO: Remove the ugly popen sally invoking here. Use a library call instead.
        libsvm_path = os.path.join(self.embeddings_directory, 'embedding.libsvm')
        p = subprocess.Popen(["sally",
                              "--config", "sally.cfg",
                              "--vect_embed", "bin", self.embeddings_data_directory,
                              libsvm_path
                              ], stdout=subprocess.PIPE)
        p.wait()
        sally_result, sally_errors = p.communicate()
        if len(sally_result) > 0 or sally_errors:
            sys.stderr.write("[-] Sally:\n")
            sys.stderr.write(p.stdout)
            sys.exit()

        self._print("[+] All embeddings have been created.")
        # Print nearest neighbors.
        self._findNearestNeighbors(node_id)



if __name__ == '__main__':
    tool = NodeExtrapolator(DESCRIPTION)
    tool.run()
