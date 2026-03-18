from __future__ import annotations
from collections import Counter
from pathlib import Path

# Apify SDK - A toolkit for building Apify Actors. Read more at:
# https://docs.apify.com/sdk/python
from apify import Actor
from apify_client import ApifyClient

import os
import httpx
from google.genai import Client as GeminiClient
from google.genai.types import (
    ThinkingConfig,
    ThinkingLevel,
    Tool,
    GenerateContentConfig,
    GoogleSearch,
)
from pydantic import BaseModel, Field, ConfigDict
from typing import List, Optional, Any, Literal
from dotenv import load_dotenv
from urllib.parse import urlparse, urlunparse
import tldextract

load_dotenv()  # Load environment variables from .env file

if not os.getenv("APIFY_TOKEN"):
    raise ValueError('Missing "APIFY_TOKEN" environment variable.')

if not os.getenv("GEMINI_API_KEY"):
    raise ValueError('Missing "GEMINI_API_KEY" environment variable.')

USE_CACHE = os.getenv("USE_CACHE", "false").lower() == "true"
CACHE_DIR = Path(__file__).parent.parent / "storage/search_results_cache"
COMPETITOR_ANALYSIS_PROMPT_TEMPLATE = (
    Path(__file__).parent / "prompts/competitor_analysis.txt"
).read_text()
LLMS_TXT_ANALYSIS_PROMPT_TEMPLATE = (
    Path(__file__).parent / "prompts/llms_txt_analysis.txt"
).read_text()

NAV_SELECTORS = [
    "nav",
    "[role=navigation]",
    ".nav",
    ".navbar",
    ".menu",
    ".header",
]

FOOTER_SELECTORS = [
    "footer",
    ".footer",
    "#footer",
]

BAD_PREFIXES = (
    "mailto:",
    "tel:",
    "javascript:",
    "#",
)

OPERATED_DOMAINS = (
    "github.com",
    "youtube.com",
    "x.com",
    "linkedin.com",
    "instagram.com",
)

SEARCH_ENGINES: tuple[Literal["google", "perplexity", "chatgpt"], ...] = (
    "google",
    "perplexity",
    "chatgpt",
)


class EngineRanking(BaseModel):
    google: Optional[int] = Field(description="Ranking for Google search engine")
    perplexity: Optional[int] = Field(
        description="Ranking for Perplexity search engine"
    )
    chatgpt: Optional[int] = Field(description="Ranking for ChatGPT search engine")


class CompetitorAnalysis(BaseModel):
    name: str = Field(description="Name of the competitor")
    website: str = Field(description="Official website of the competitor")
    ranking: EngineRanking = Field(
        description="Ranking of the competitor across different search engines"
    )


class CompetitorRanking(BaseModel):
    search_query: Optional[str] = None
    competitors: List[CompetitorAnalysis] = Field(
        description="List of competitors and their rankings"
    )


class GeminiSource(BaseModel):
    id: int
    url: str
    title: str
    description: str
    section: str


class GeminiSearchResult(BaseModel):
    text: str
    sources: List[GeminiSource]
    query: str
    url: str
    provider: Literal["Google"]


class PerplexitySource(BaseModel):
    title: str
    url: str
    snippet: str
    date: Optional[str] = None
    lastUpdated: Optional[str] = None
    source: str


class PerplexitySearchResult(BaseModel):
    text: str
    sources: List[PerplexitySource]
    citationUrls: List[str]
    relatedQuestions: List[str]
    images: List[str]
    query: str
    provider: Literal["Perplexity"]


class ChatGptSource(BaseModel):
    id: str
    title: str
    url: str
    section: str
    description: Optional[str] = None


class ChatGptSearchResult(BaseModel):
    text: str
    queryFanOut: List[str]
    widgets: List[Any]
    sources: List[ChatGptSource]
    query: str
    provider: Literal["OpenAI"]


class SearchResult(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    gemini: GeminiSearchResult = Field(alias="aiModeResult")
    perplexity: PerplexitySearchResult = Field(alias="perplexitySearchResult")
    chatgpt: ChatGptSearchResult = Field(alias="chatGptSearchResult")


class LLMsTxtSummary(BaseModel):
    name: str = Field(
        description="Name of the product or service mentioned in the llms.txt file"
    )
    description: str = Field(
        description="Description of the product or service mentioned in the llms.txt file"
    )
    urls: List[str] = Field(
        description="List of URLs associated with the product or service mentioned in the llms.txt file"
    )


class LLMsTxtGapAnalysis(BaseModel):
    gaps: List[str] = Field(
        description="List of identified gaps in the llms.txt configuration"
    )
    recommendations: List[str] = Field(
        description="List of recommendations to improve the llms.txt configuration"
    )


class LLMsTxtAnalysis(BaseModel):
    summary: LLMsTxtSummary = Field(
        description="Summary of the product mentioned in the llms.txt file"
    )
    score: int = Field(
        description="Overall score out of 100 for the llms.txt configuration based on the analysis"
    )
    gap_analysis: LLMsTxtGapAnalysis = Field(
        description="Analysis of the llms.txt file, including identified gaps and recommendations"
    )


class URLSelection(BaseModel):
    relevant_urls: List[str] = Field(
        description="List of relevant URLs that should be kept for further analysis"
    )


class SourceUrl(BaseModel):
    url: str
    domain: str
    category: Literal["owned", "operated", "earned"]
    mentions: int


class DomainMentions(BaseModel):
    domain: str
    mentions: int


class QueryVisibilityScore(BaseModel):
    query: str
    google: float
    perplexity: float
    chatgpt: float
    average: float


class EngineVisibilityScore(BaseModel):
    google: float
    perplexity: float
    chatgpt: float


class VisibilityScore(BaseModel):
    overall: float
    per_engine: EngineVisibilityScore
    per_query: List[QueryVisibilityScore]


class SourcesAnalysis(BaseModel):
    mentions_by_domain: List[DomainMentions]
    sources: List[SourceUrl]


gemini_client = GeminiClient(api_key=os.getenv("GEMINI_API_KEY"))
apify_client = ApifyClient(token=os.getenv("APIFY_TOKEN"))


def to_registrable_domain(value: str) -> str:
    parsed = urlparse(value if "://" in value else f"https://{value}")
    host = parsed.netloc or parsed.path
    ext = tldextract.extract(host)
    if not ext.domain or not ext.suffix:
        return ""
    return f"{ext.domain}.{ext.suffix}".lower()


def get_search_result_cache_path(search_query: str) -> Path:
    normalized_query = " ".join(search_query.strip().lower().split())
    if not normalized_query:
        raise ValueError('Missing "search_query" value.')

    safe_query = normalized_query.replace(" ", "_")
    return CACHE_DIR / f"{safe_query}.json"


def scrape_website(url: str) -> dict[str, Any]:
    if not url:
        raise ValueError('Missing "url" value.')

    run_input = {
        "aggressivePrune": True,
        "blockMedia": True,
        "clickElementsCssSelector": '[aria-expanded="false"]',
        "clientSideMinChangePercentage": 15,
        "crawlerType": "playwright:adaptive",
        "debugLog": False,
        "debugMode": False,
        "dynamicContentWaitSecs": 2,
        "expandIframes": True,
        "ignoreCanonicalUrl": False,
        "ignoreHttpsErrors": False,
        "keepUrlFragments": False,
        "maxCrawlDepth": 5,
        "maxCrawlPages": 10,
        "proxyConfiguration": {
            "useApifyProxy": True,
            "apifyProxyGroups": ["RESIDENTIAL"],
        },
        "readableTextCharThreshold": 100,
        "removeCookieWarnings": True,
        "removeElementsCssSelector": 'nav, footer, script, style, noscript, svg, img[src^=\'data:\'],\n[role="alert"],\n[role="banner"],\n[role="dialog"],\n[role="alertdialog"],\n[role="region"][aria-label*="skip" i],\n[aria-modal="true"]',
        "renderingTypeDetectionPercentage": 10,
        "respectRobotsTxtFile": True,
        "reuseStoredDetectionResults": False,
        "saveFiles": False,
        "saveHtml": False,
        "saveHtmlAsFile": False,
        "saveMarkdown": True,
        "saveScreenshots": False,
        "signHttpRequests": False,
        "startUrls": [{"url": url}],
        "storeSkippedUrls": False,
        "useLlmsTxt": True,
        "useSitemaps": False,
    }

    Actor.log.info(f"Scraping website content from {url}")
    run = apify_client.actor("apify/website-content-crawler").call(run_input=run_input)
    dataset = apify_client.dataset(run["defaultDatasetId"])
    items = dataset.list_items().items

    return items[0]


def fetch_search_result(search_query: str) -> SearchResult:
    if not search_query:
        raise ValueError('Missing "search_query" value.')

    cache_path = get_search_result_cache_path(search_query)

    if USE_CACHE and cache_path.exists():
        Actor.log.info(
            f"Loading search result from local cache for query: {search_query}"
        )
        return SearchResult.model_validate_json(cache_path.read_text())

    run_input = {
        "aiModeSearch": {"enableAiMode": True},
        "chatGptSearch": {"enableChatGpt": True},
        "countryCode": "us",
        "disableGoogleSearchResults": True,
        "focusOnPaidAds": False,
        "forceExactMatch": False,
        "includeIcons": False,
        "includeUnfilteredResults": False,
        "languageCode": "en",
        "maxPagesPerQuery": 1,
        "maximumLeadsEnrichmentRecords": 0,
        "mobileResults": False,
        "perplexitySearch": {
            "enablePerplexity": True,
            "returnImages": False,
            "returnRelatedQuestions": False,
        },
        "queries": search_query,
        "resultsPerPage": 100,
        "saveHtml": False,
        "saveHtmlToKeyValueStore": True,
        "searchLanguage": "en",
    }

    Actor.log.info("Running apify/google-search-scraper actor")
    run = apify_client.actor("apify/google-search-scraper").call(run_input=run_input)
    dataset = apify_client.dataset(run["defaultDatasetId"])
    items = dataset.list_items().items

    result = SearchResult.model_validate(items[0])

    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    cache_path.write_text(result.model_dump_json(by_alias=True))
    Actor.log.info(f"Search result cached to {cache_path}")

    return result


def rank_competitors(gemini: str, perplexity: str, chatgpt: str) -> CompetitorRanking:
    Actor.log.info("Analyzing competitors based on search results")

    if not gemini:
        raise ValueError('Missing "gemini" value.')
    if not perplexity:
        raise ValueError('Missing "perplexity" value.')
    if not chatgpt:
        raise ValueError('Missing "chatgpt" value.')

    prompt = COMPETITOR_ANALYSIS_PROMPT_TEMPLATE.format(
        google=gemini,
        perplexity=perplexity,
        chatgpt=chatgpt,
    )

    response = gemini_client.models.generate_content(
        model="gemini-3.1-flash-lite-preview",
        contents=prompt,
        config=GenerateContentConfig(
            response_mime_type="application/json",
            response_json_schema=CompetitorRanking.model_json_schema(),
            tools=[Tool(google_search=GoogleSearch())],
        ),
    )

    if response.text is None:
        raise RuntimeError("Gemini competitor analysis returned no text.")

    return CompetitorRanking.model_validate_json(response.text)


def get_llms_txt(url: str) -> Optional[str]:
    if not url:
        raise ValueError('Missing "url" value.')

    with httpx.Client(follow_redirects=True) as client:
        response = client.get(f"{url}/llms.txt")
        if response.status_code == 200:
            Actor.log.info(f"Successfully fetched llms.txt from {url}")
            return response.text
        else:
            Actor.log.warning(
                f"Failed to fetch llms.txt from {url}. Status code: {response.status_code}"
            )
            return None


def analyze_llms_txt(llms_txt: str) -> LLMsTxtAnalysis:
    Actor.log.info("Analyzing llms.txt content")

    if not llms_txt:
        raise ValueError('Missing "llms_txt" value.')

    prompt = LLMS_TXT_ANALYSIS_PROMPT_TEMPLATE.format(
        llms_txt=llms_txt,
    )

    response = gemini_client.models.generate_content(
        model="gemini-3.1-flash-lite-preview",
        contents=prompt,
        config=GenerateContentConfig(
            response_mime_type="application/json",
            response_json_schema=LLMsTxtAnalysis.model_json_schema(),
            thinking_config=ThinkingConfig(thinking_level=ThinkingLevel.HIGH),
        ),
    )

    if response.text is None:
        raise RuntimeError("Gemini llms.txt analysis returned no text.")

    return LLMsTxtAnalysis.model_validate_json(response.text)


def calculate_visibility_score(
    competitor_rankings: List[CompetitorRanking], input_url: str
) -> VisibilityScore:
    owned_domain = to_registrable_domain(input_url)
    if not owned_domain:
        raise ValueError(f'Could not derive domain from input_url="{input_url}".')

    if not competitor_rankings:
        return VisibilityScore(
            overall=0.0,
            per_engine=EngineVisibilityScore(google=0.0, perplexity=0.0, chatgpt=0.0),
            per_query=[],
        )

    def _relative_score(rank: Optional[int], ranked_count: int) -> float:
        """
        Score relative to competitors in the same engine list.

        - If present: percentile-like score where rank 1 = 100.
        - If missing: assign a soft baseline equivalent to being just below the observed list
          so the metric is less punitive than hard zero.
        """
        if ranked_count <= 0:
            return 0.0

        if rank is None:
            return round(100.0 / (ranked_count + 1), 2)

        clamped_rank = min(max(rank, 1), ranked_count)
        return round(((ranked_count - clamped_rank + 1) / ranked_count) * 100, 2)

    per_query: list[QueryVisibilityScore] = []
    per_engine_buckets: dict[str, list[float]] = {
        engine: [] for engine in SEARCH_ENGINES
    }

    for ranking in competitor_rankings:
        best_rank_by_engine: dict[str, Optional[int]] = {
            "google": None,
            "perplexity": None,
            "chatgpt": None,
        }

        for competitor in ranking.competitors:
            if to_registrable_domain(competitor.website) != owned_domain:
                continue

            engine_to_rank = {
                "google": competitor.ranking.google,
                "perplexity": competitor.ranking.perplexity,
                "chatgpt": competitor.ranking.chatgpt,
            }
            for engine, rank in engine_to_rank.items():
                if rank is None:
                    continue
                current = best_rank_by_engine[engine]
                if current is None or rank < current:
                    best_rank_by_engine[engine] = rank

        ranked_count_by_engine: dict[str, int] = {
            "google": 0,
            "perplexity": 0,
            "chatgpt": 0,
        }
        for competitor in ranking.competitors:
            if competitor.ranking.google is not None:
                ranked_count_by_engine["google"] += 1
            if competitor.ranking.perplexity is not None:
                ranked_count_by_engine["perplexity"] += 1
            if competitor.ranking.chatgpt is not None:
                ranked_count_by_engine["chatgpt"] += 1

        score_by_engine: dict[str, float] = {}
        for engine in SEARCH_ENGINES:
            rank = best_rank_by_engine[engine]
            score = _relative_score(rank, ranked_count_by_engine[engine])
            score_by_engine[engine] = score
            per_engine_buckets[engine].append(score)

        query_average = round(sum(score_by_engine.values()) / len(SEARCH_ENGINES), 2)
        per_query.append(
            QueryVisibilityScore(
                query=ranking.search_query or "unknown",
                google=score_by_engine["google"],
                perplexity=score_by_engine["perplexity"],
                chatgpt=score_by_engine["chatgpt"],
                average=query_average,
            )
        )

    per_engine = EngineVisibilityScore(
        google=(
            round(
                sum(per_engine_buckets["google"]) / len(per_engine_buckets["google"]), 2
            )
            if per_engine_buckets["google"]
            else 0.0
        ),
        perplexity=(
            round(
                sum(per_engine_buckets["perplexity"])
                / len(per_engine_buckets["perplexity"]),
                2,
            )
            if per_engine_buckets["perplexity"]
            else 0.0
        ),
        chatgpt=(
            round(
                sum(per_engine_buckets["chatgpt"]) / len(per_engine_buckets["chatgpt"]),
                2,
            )
            if per_engine_buckets["chatgpt"]
            else 0.0
        ),
    )

    all_scores = [score for scores in per_engine_buckets.values() for score in scores]
    overall = round(sum(all_scores) / len(all_scores), 2) if all_scores else 0.0

    return VisibilityScore(
        overall=overall,
        per_engine=per_engine,
        per_query=per_query,
    )


def get_source_urls(
    search_results: List[SearchResult], input_url: str
) -> List[SourceUrl]:
    owned_domain = to_registrable_domain(input_url)
    if not owned_domain:
        raise ValueError(f'Could not derive domain from input_url="{input_url}".')

    normalized_operated = {
        to_registrable_domain(domain)
        for domain in (OPERATED_DOMAINS or set())
        if to_registrable_domain(domain)
    }

    all_raw_urls: List[str] = []
    for search_result in search_results:
        all_raw_urls.extend(source.url for source in search_result.gemini.sources)
        all_raw_urls.extend(source.url for source in search_result.perplexity.sources)
        all_raw_urls.extend(source.url for source in search_result.chatgpt.sources)

    def _canonicalize_url(raw_url: str) -> str:
        parsed = urlparse(raw_url.strip())

        if not parsed.scheme or not parsed.netloc:
            return ""

        clean = parsed._replace(params="", query="", fragment="")
        url = urlunparse(clean)
        return url.rstrip("/")

    canonical_urls = [_canonicalize_url(u) for u in all_raw_urls]
    canonical_urls = [u for u in canonical_urls if u]
    counts = Counter(canonical_urls)

    classified: List[SourceUrl] = []
    for canonical_url, mentions in counts.items():
        source_domain = to_registrable_domain(canonical_url)
        if not source_domain:
            continue

        if source_domain == owned_domain:
            category: Literal["owned", "operated", "earned"] = "owned"
        elif source_domain in normalized_operated:
            category = "operated"
        else:
            category = "earned"

        classified.append(
            SourceUrl(
                url=canonical_url,
                domain=source_domain,
                category=category,
                mentions=mentions,
            )
        )

    return classified


class Report(BaseModel):
    llms_txt_analysis: LLMsTxtAnalysis
    sources_analysis: SourcesAnalysis
    competitor_rankings: List[CompetitorRanking]
    visibility_score: VisibilityScore


async def main() -> None:
    async with Actor:
        Actor.log.info(
            f"Starting CiteShift Actor run (cache {'enabled' if USE_CACHE else 'disabled'})"
        )

        actor_input = await Actor.get_input()

        url = actor_input.get("url")
        search_queries = actor_input.get("search_queries")

        Actor.log.info(
            f"Actor input received: url={url}, search_queries={search_queries}"
        )

        if not url:
            raise ValueError('Missing "url" attribute in input!')
        if not search_queries:
            raise ValueError('Missing "search_queries" attribute in input!')

        # ANALYZE LLMS.TXT
        llms_txt = get_llms_txt(url) or ""
        llms_txt_analysis = analyze_llms_txt(llms_txt)
        Actor.log.info(
            f"LLMs.txt Analysis Score: {llms_txt_analysis.score}/100\n"
            f"Gaps: {', '.join(llms_txt_analysis.gap_analysis.gaps)}\n"
            f"Recommendations: {', '.join(llms_txt_analysis.gap_analysis.recommendations)}"
        )

        search_results: list[SearchResult] = []
        competitor_rankings: list[CompetitorRanking] = []
        for search_query in search_queries:
            try:
                search_result = fetch_search_result(search_query)
                search_results.append(search_result)

                competitor_ranking = rank_competitors(
                    gemini=search_result.gemini.text,
                    perplexity=search_result.perplexity.text,
                    chatgpt=search_result.chatgpt.text,
                )
                competitor_ranking.search_query = search_query
                competitor_rankings.append(competitor_ranking)
            except Exception as error:
                Actor.log.error(
                    f"Failed to fetch search result for query '{search_query}': {error}"
                )

        visibility_score = calculate_visibility_score(competitor_rankings, url)
        Actor.log.info(
            f"Visibility Score: {visibility_score.overall}/100 "
            f"(Google={visibility_score.per_engine.google}, "
            f"Perplexity={visibility_score.per_engine.perplexity}, "
            f"ChatGPT={visibility_score.per_engine.chatgpt})"
        )

        sources = get_source_urls(search_results, url)
        Actor.log.info(
            f"Found {len(sources)} unique source URLs from {len(search_results)} queries."
            f"  Owned: {len([s for s in sources if s.category == 'owned'])}"
            f"  Operated: {len([s for s in sources if s.category == 'operated'])}"
            f"  Earned: {len([s for s in sources if s.category == 'earned'])}"
        )

        mentions_counter: Counter[str] = Counter()
        for source in sources:
            mentions_counter[source.domain] += source.mentions

        mentions_by_domain = [
            DomainMentions(domain=domain, mentions=mentions)
            for domain, mentions in mentions_counter.most_common()
        ]

        report = Report(
            llms_txt_analysis=llms_txt_analysis,
            sources_analysis=SourcesAnalysis(
                mentions_by_domain=mentions_by_domain,
                sources=sources,
            ),
            competitor_rankings=competitor_rankings,
            visibility_score=visibility_score,
        )

        report_path = Path("report.json")
        report_path.write_text(report.model_dump_json(indent=2), encoding="utf-8")
        Actor.log.info(f"Report saved to {report_path.resolve()}")

        # mentions_by_domain: Counter[str] = Counter()
        # for source in sources:
        #     mentions_by_domain[source.domain] += source.mentions

        # for domain, mentions in mentions_by_domain.most_common():
        #     Actor.log.info(f"{domain}: {mentions}")
