from job_finder.search_configuration import (
    DEFAULT_SEARCH_CONFIGURATION,
    SearchQuery,
    SupportedSearchSource,
    build_search_queries,
)


def test_search_queries_preserve_configuration_order_and_rendering() -> None:
    configuration = DEFAULT_SEARCH_CONFIGURATION.model_copy(
        update={
            "search_keywords": ("product engineer", "applied AI engineer"),
            "enabled_sources": (
                SupportedSearchSource.LEVER,
                SupportedSearchSource.ASHBY,
            ),
        }
    )

    queries = build_search_queries(configuration)

    assert queries == (
        SearchQuery(keyword="product engineer", domain="jobs.lever.co"),
        SearchQuery(keyword="product engineer", domain="jobs.ashbyhq.com"),
        SearchQuery(keyword="applied AI engineer", domain="jobs.lever.co"),
        SearchQuery(keyword="applied AI engineer", domain="jobs.ashbyhq.com"),
    )
    assert tuple(query.text for query in queries) == (
        "site:jobs.lever.co product engineer",
        "site:jobs.ashbyhq.com product engineer",
        "site:jobs.lever.co applied AI engineer",
        "site:jobs.ashbyhq.com applied AI engineer",
    )
