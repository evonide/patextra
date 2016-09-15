
/**
 * Get all by a patch affected patch file nodes.
 *  @param patch_node_id id of the patch file node.
 * */
getPatchAffectedFiles = { String patch_node_id ->
    patch_node = g.v(patch_node_id);
    return patch_node.out('affects').id;
}

// TODO: finish this...
getCalleeInfos = { String callee_id ->
    _().functions().location
}

/**
 * Retrieve all callees for a specific patch file node.
 *  @param patch_file_node_id id of the patch file node.
 * */
getPatchOperations = { String patch_file_node_id ->
    patch_file_node = g.v(patch_file_node_id);
    patch_operation_nodes = patch_file_node.out('applies').out('replaces').toList();

    if(patch_operation_nodes.size == 0)
        return;

    patch_operation_nodes._().transform{
        it._().statements().children().out.dedup().loop(2){true}{true}.filter{
            it.nodeType == 'Callee' && it.code.length() > 0
        }.toList()
    }.filter{it.size > 0}.transform{
        it[0].id
    }
}