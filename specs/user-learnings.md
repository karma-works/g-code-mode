# Learnings

This page records implementation and vendor learnings for GapHunter. Add new dated entries here when a product, API, deployment, or research assumption changes.

## 2026-05-14: Google Search Providers

### Custom Search JSON API

Source: [Custom Search JSON API overview](https://developers.google.com/custom-search/v1/overview)

Key findings:

- Custom Search JSON API can return JSON search results from a configured Programmable Search Engine, but it requires both an API key and a search engine ID.
- Google documents the API as closed to new customers.
- Existing customers have until January 1, 2027 to transition to an alternative solution.
- Published legacy pricing is 100 free queries per day, then $5 per 1,000 queries up to 10,000 queries per day.
- Service account authentication is not a practical workaround for GapHunter's current Custom Search integration. The JSON API path is API-key based, and our tests with project-level enablement plus API keys still returned access-denied responses for the project.

Project implication:

Custom Search JSON API should not be treated as GapHunter's durable live-web research backend. Keep the adapter as a compatibility path for accounts that already have API access, but design the search boundary so another provider can be swapped in.

Cleanup action:

- Deleted the Google Cloud API keys that were restricted to `customsearch.googleapis.com`.
- Removed the `GOOGLE_CUSTOM_SEARCH_API_KEY` GitHub secret.
- Removed the `JOB_SEARCH_ENGINE_ID` and `COMPETITOR_SEARCH_ENGINE_ID` GitHub variables.
- Removed the Custom Search environment variables from the deployed Cloud Run service.
- Removed Custom Search credential injection from GitHub Actions deployment.

### Vertex AI Search / Agent Search

Sources:

- [Migrate from Custom Search Site Restricted JSON API](https://docs.cloud.google.com/generative-ai-app-builder/docs/migrate-from-cse)
- [Agent Search pricing](https://cloud.google.com/generative-ai-app-builder/pricing)

Key findings:

- Google is renaming Vertex AI Search to Agent Search in current documentation. Some Google Cloud pages, release notes, and older articles still use names such as Vertex AI Search, Vertex AI Search and Conversation, Agent Builder, Enterprise Search, or Generative AI App Builder. Treat these as the same product family until a concrete API path proves otherwise.
- Agent Search is Google's recommended path for site-restricted search, not a drop-in live open-web replacement.
- Website search requires creating an Agent Search app and website data store, then configuring URL patterns to index.
- Website search uses Enterprise edition features.
- Advanced website indexing and richer generated answers can require domain verification and add indexing costs.
- Authentication differs by API path:
  - `searchLite` can use an API key.
  - `search` and `answer` use OAuth 2.0 and appropriate Discovery Engine IAM roles.
- General pricing currently includes a 10,000 query per account per month free trial, then $1.50 per 1,000 Standard search queries or $4.00 per 1,000 Enterprise search queries. Advanced generative answers add $4.00 per 1,000 user input queries.
- Index storage has a 10 GiB monthly free quota. Above that, listed storage pricing is based on indexed raw data. For website data stores, Google estimates storage as 500 KiB per indexed page, with a 1,000-page website example at about $2.38 per month before free quota effects.

Project implication:

Agent Search can work if GapHunter narrows research to curated source lists such as specific job boards, review sites, competitor directories, and domain allowlists. It is weaker for broad discovery across the open web because the system must first know what sites to index. Runtime query latency should be suitable for product use after indexing, but setup, indexing freshness, source coverage, and cost monitoring become part of the product architecture.

Decision guidance:

- Use Agent Search if Google-native infrastructure, controlled source coverage, IAM, and indexed-site quality matter more than open-web breadth.
- Use another web search provider if GapHunter needs broad web discovery, faster MVP setup, or lower operational complexity.
- Keep provider-specific details behind a search adapter so Custom Search, Agent Search, and non-Google search providers can coexist during evaluation.

## 2026-05-14: Brave Search vs SerpAPI

Sources:

- [Brave Search API](https://brave.com/search/api/)
- [Brave Web Search API docs](https://api-dashboard.search.brave.com/app/documentation/web-search/get-started)
- [SerpAPI Google Search API](https://serpapi.com/search-api)
- [SerpAPI pricing](https://serpapi.com/pricing)

Both providers return structured JSON.

Brave Search:

- Returns JSON from its own independent web index.
- Web results are exposed under structured fields such as `web.results`, with `title`, `url`, `description`, and optional `extra_snippets`.
- Supports freshness filters, country/language targeting, safe search, site operators, pagination, Goggles for custom ranking/filtering, news/images/video endpoints, and an LLM Context endpoint intended for machine consumption.
- Pricing is request-based. Current public pricing lists Search at $5 per 1,000 requests with monthly free credits, and Answers at $4 per 1,000 requests plus token charges.
- Best fit for GapHunter if we want broad web research, AI-oriented context, predictable integration, and less dependency on Google SERP scraping.

SerpAPI:

- Returns structured JSON parsed from search engine result pages.
- Google Search results include structured sections such as `organic_results`, local results, ads, knowledge graph, answer boxes, images, news, shopping, videos, and metadata.
- Covers many engines and verticals beyond Google Search, including Google Jobs, Google News, Google Maps, YouTube, Amazon, DuckDuckGo, Bing, Yahoo, and more.
- Pricing is monthly quota-based. Current public pricing lists a free tier with 250 searches per month, then paid tiers such as $25/month for 1,000 searches, $75/month for 5,000 searches, $150/month for 15,000 searches, and $275/month for 30,000 searches.
- Best fit for GapHunter if we specifically need Google-like SERP features, Google Jobs-style verticals, or many specialized search engines through one API.

Decision guidance:

- Prefer Brave Search for the MVP live research backend. It is simpler, directly web-search oriented, AI-app friendly, and not tied to scraping Google SERPs. This decision is now recorded in [ADR-004](../specs/ADR-004-web-search.md).
- Choose SerpAPI if GapHunter's quality depends on Google-specific result modules, especially jobs, maps/local, shopping, trends, or rich SERP features.
- Implement the next search adapter with a provider setting, for example `SEARCH_PROVIDER=brave|serpapi|custom_search|demo`, so provider choice remains reversible.
