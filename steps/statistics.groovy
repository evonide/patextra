

/**
 * Get the overall patch sum for each of the provided patch properties.
 *  @param required_fields [str] the requested patch properties to be aggregated.
 * */
// TODO: include this in the patch statistics utility...
getAggregatedPatchProperties = { def properties ->
    patch_nodes = queryPatchIndex("/.*patch/").toList();
    results = []
    for (String property: properties) {
        results += [patch_nodes._().property.sum()];
    }
    return results
}