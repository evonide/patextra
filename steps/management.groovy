import com.orientechnologies.orient.core.record.impl.ODocument;

/**
 * Execute a sql query and fetch its results.
 *  @param query query that is supposed to be executed.
 * */
execSQLQuery = { def queryString ->
    System.out.println("[->] Query: " + queryString + "\n")
    def query = new com.orientechnologies.orient.core.sql.OCommandSQL(queryString);
    def db = g.getRawGraph();
    db.activateOnCurrentThread();
    return db.command(query).execute().toList()._().transform{ g.v(it.getIdentity()) }
}

/**
 * Escape a query for usage in Lucene fulltext lookups. Very useful for looking up filepaths.
 *  @param query query that is supposed to be escaped.
 * */
escapeLuceneQuery = { String query ->
    // Attention: Lucene needs proper escaping in addition to the requirement of KeywordAnalyzer...
    // c.f. https://lucene.apache.org/core/2_9_4/queryparsersyntax.html#Escaping%20Special%20Characters
    return query.replaceAll("(?=[]\\[.+&|!(){}^\"~*?:\\\\\\-/])", "\\\\\\\\");
}

/**
 Retrieve nodes from index using a Lucene query.
 @param query The lucene query to run
 @param no_escape True, if the query is supposed to be passed unescaped.
 */
queryPatchIndexHelper = { def luceneQuery, def escaped ->
    if (escaped)
        luceneQuery = escapeLuceneQuery(luceneQuery);
    sqlQuery = 'SELECT * FROM V WHERE filepath LUCENE "' + luceneQuery + '" AND nodeType="Patch"';
    return execSQLQuery(sqlQuery);
}

/**
 Retrieve nodes from index using a Lucene query.
 @param query The lucene query to run
 */
queryPatchIndex = { def query ->
    return queryPatchIndexHelper(query, false);
}

/**
 Retrieve nodes from index using a Lucene query.
 @param query The lucene query to run
 */
queryPatchIndexEscaped = { def query ->
    return queryPatchIndexHelper(query, true);
}

/**
 Retrieve the node id of a patched file if existent.
 @param query The expected filepath of the patch file.
 */
queryFileByPath = { def filepath, def workaround ->
    filepath = escapeLuceneQuery(filepath);
    // TODO: remove workaround against strange OrientDB behavior with long LUCENE queries :(...
    if (workaround)
        filepath = '/.*' + filepath + '.*/';
    sqlQuery = 'SELECT * FROM V WHERE filepath LUCENE "' + filepath + '" AND nodeType="File"';
    return execSQLQuery(sqlQuery);
}

//patch_nodes = queryPatchIndex("/.*patch/")
//patch_nodes._().transform{it.id + "\t" + it.out.out('applies').sideEffect{it.linesAdded-it.linesRemoved}}

/**
 Retrieve all patches in a sorted order.
 */
get_all_patches = {
    queryPatchIndex("/.*patch/").sort{
        it.linesAdded - it.linesRemoved
    }._().sideEffect{
        delta = it.linesAdded - it.linesRemoved
    }.transform {
        "" + it.id + "\t" + it.filepath + "\t" + delta + "\t" + it.reversed
    }
}

/**
 * Get all nodes from a specific start line to a specific end line starting at a file node.
 *  @param file_node id of the file node
 *  @param start_line line number where code segment starts
 *  @param end_line line number where code segment ends
 * */
getCodeNodes = {  String file_node_id,  Integer start_line,  Integer end_line ->
    g.v(file_node_id).out.dedup().loop(2){true}{true}.has('location').sideEffect {
        location_tokens = it.location.tokenize(':')
        in_line = -1
        // Verify if location is non-emtpy and only then use the correct line number.
        if (location_tokens.size > 0)
            in_line = location_tokens[0].toInteger()
    }.filter{
        in_line >= start_line && in_line <= end_line
    }
    /*
    .filter{
        it.type != 'Function' && it.type != 'CompoundStatement' && it.type != 'Parameter'
    }
    */
}

/**
 * Cleanup any previous patch effects.
 *  @param patch_node_id id of the patch file node.
 * */
cleanupPatchEffects = {String patch_node_id ->
    patch_node = g.v(patch_node_id);
    // Get all patch file nodes.
    patch_file_nodes = patch_node.out('affects').toList();
    // Remove any patch operations.
    patch_file_nodes._().out('applies').remove();
    // Cleanup all patch file nodes now.
    patch_file_nodes._().remove();
    g.commit();
}

/**
 * Create an operation node (e.g. 'removes) and connect it to the patch file node.
 *  @param patch_file_node_id id of the patch file node.
 *  @param file_node_id id of the affected file.
 *  @param operation The name of the operation that is going to be performed.
 *  @param start_line line number where code segment starts
 *  @param end_line line number where code segment ends
 * */
connectPatchWithAffectedCode = { String patch_file_node_id, String file_node_id, String hunk_node_id, String operation, Integer line_start, Integer line_end ->
    patch_file_node = g.v(patch_file_node_id);
    hunk_node = g.v(hunk_node_id);

    // Get all affected nodes and connect the patch file node with them.
    affectedLineNodes = getCodeNodes(file_node_id, line_start, line_end).toList();
    affectedLineNodes.each{hunk_node.addEdge(operation, it)}

    g.commit();
    return affectedLineNodes.size;
}

/**
 * Create a patch index if it doesn't exist yet.
 * */
createPatchIndex = {
    def db = g.getRawGraph();

    // Create the patch index only if it doesn't exist yet.
    if (!db.getMetadata().getIndexManager().existsIndex("patchIndex.")) {
        indexKeys = [(PATCH_FILEPATH)] as String[]
        def nodeClass = db.getMetadata().getSchema().getClass("V");
        metadata = new ODocument();
        metadata.field("analyzer", "org.apache.lucene.analysis.core.KeywordAnalyzer");
        metadata.field("index_analyzer", "org.apache.lucene.analysis.core.KeywordAnalyzer");
        metadata.field("query_analyzer", "org.apache.lucene.analysis.core.KeywordAnalyzer");
        metadata.field("default", "org.apache.lucene.analysis.core.KeywordAnalyzer");
        metadata.field("index", "org.apache.lucene.analysis.core.KeywordAnalyzer");
        metadata.field("query", "org.apache.lucene.analysis.core.KeywordAnalyzer");
        metadata.field("indexRadix", true);
        metadata.field("stopWords", []);
        metadata.field("separatorChars", "");
        metadata.field("ignoreChars", "");
        metadata.field("minWordLength", 1);
        nodeClass.createIndex("patchIndex.", "FULLTEXT", null, metadata, "LUCENE", indexKeys);
    }
}

/**
 * Create a new patch node in the database.
 *  @param filepath The path of the patch file.
 *  @param description A description of the patch.
 * */
createPatchNode = { String filepath, String description ->
    // Ensure the patch index exists.
    createPatchIndex();
    // Select all patch-nodes with our patchIndex and the provided filepath.
    result_nodes = queryPatchIndexEscaped(filepath).toList();
    number_of_vertices = result_nodes.size

    // Check if this patch already exists in the database.
    if(number_of_vertices == 0) {
        // Create a new node in the database.
        new_node = g.addVertex([(PATCH_FILEPATH): filepath,
                                (PATCH_DESCRIPTION): description,
                                (PATCH_TYPE): 'Patch'])
        // Return the id of our new node.
        return new_node.id
    } else if(number_of_vertices == 1) {
        // Return the existing node's id.
        return result_nodes[0].id
    } else {
        // The node exists more than once. Delete all occurences for now.
        for (entry in result_nodes) {
            cleanupPatchEffects(entry.id);
            // Remove this node from the Neo4j database.
            g.removeVertex(entry);
        }
        // Return -1 to signal an error...
        return -1
    }
}
