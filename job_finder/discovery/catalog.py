from enum import StrEnum


class SupportedSearchSource(StrEnum):
    ASHBY = "ashby"
    LEVER = "lever"
    GREENHOUSE = "greenhouse"
    WORKABLE = "workable"


SEARCH_SOURCE_DOMAINS: dict[SupportedSearchSource, str] = {
    SupportedSearchSource.ASHBY: "jobs.ashbyhq.com",
    SupportedSearchSource.LEVER: "jobs.lever.co",
    SupportedSearchSource.GREENHOUSE: "boards.greenhouse.io",
    SupportedSearchSource.WORKABLE: "apply.workable.com",
}
SEARCH_DOMAINS = tuple(SEARCH_SOURCE_DOMAINS.values())

SEARCH_KEYWORDS = (
    "senior product engineer",
    "staff product engineer",
    "lead product engineer",
    "founding engineer",
    "founding full stack engineer",
    "founding product engineer",
    "senior full stack engineer startup",
    "senior fullstack engineer typescript react",
    "product engineer typescript",
    "product engineer react",
    "senior software engineer seed stage",
    "senior engineer series a startup",
    "senior full stack engineer next.js",
    "senior typescript engineer product team",
    "senior node.js engineer product",
    "full stack engineer 0 to 1",
    "senior AI engineer",
    "senior engineer LLM",
    "applied AI engineer",
    "AI product engineer",
    "founding AI engineer",
    "senior backend engineer AI",
    "fullstack engineer AI",
    "senior engineer RAG",
    "senior engineer AI agents",
    "senior engineer AI platform",
    "LLM application engineer",
    "AI engineer typescript",
    "senior engineer LLM evaluation",
    "senior engineer agentic workflows",
    "AI engineer node.js",
    "senior engineer LLM products",
)
