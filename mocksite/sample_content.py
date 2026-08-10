"""Sample case studies, blog posts and testimonials for the mock site.

Requirements 2 and 6 expect the agent to show portfolios, case studies,
testimonials and blogs. The real site has none -- post-sitemap.xml returns 200
with zero entries -- so the capability cannot be demonstrated against it.

Every item here is labelled [SAMPLE] in the page body, the <title>, and the KB
block. That label is load-bearing, not decoration:

  * Ingesting third-party case studies would have the agent claim another
    company's results as Systematic IT's own. That is a factual claim about
    past work, and it is worse than a wrong price -- a price is negotiable, a
    fabricated client outcome is not.

  * Unlabelled invented content has the same problem in a quieter form.

So the content is written from the services the site genuinely sells, and
marked as illustrative. When the client supplies real material, replace the
dicts below; nothing else changes.
"""

from __future__ import annotations

SAMPLE_BADGE = (
    '<div class="note"><strong>[SAMPLE CONTENT]</strong> Illustrative example '
    "written for development. Not a real client engagement or published "
    "article.</div>"
)

CASE_STUDIES = [
    {
        "slug": "local-seo-multi-location-retail",
        "title": "[SAMPLE] Local SEO for a multi-location retailer",
        "description": "Illustrative example of a Local SEO and Google Business Profile engagement.",
        "service": "/seo/local-seo",
        "body": """
## The situation

A retailer with four branches was invisible in map results outside its main
neighbourhood. Each branch had an unclaimed or duplicated Google Business
Profile, and store pages shared one generic description.

## What was done

- Claimed and de-duplicated every Google Business Profile
- Wrote distinct location pages with genuine service-area detail
- Built consistent citations across local directories
- Added review prompts to the post-purchase flow

## Services involved

Local SEO, Google Business Profile optimisation, on-page Website SEO.

## Why it is here

This page exists so the agent can demonstrate navigating a visitor to a
relevant case study. The engagement is illustrative.
""",
    },
    {
        "slug": "shopify-store-build-and-seo",
        "title": "[SAMPLE] Shopify build with SEO groundwork",
        "description": "Illustrative example of a Shopify development and Shopify SEO engagement.",
        "service": "/development/shopify-website",
        "body": """
## The situation

A brand selling through social channels needed a storefront of its own, with
search visibility built in from the start rather than bolted on later.

## What was done

- Built the storefront on Shopify with a conversion-first product template
- Structured collections around how customers actually search
- Set up technical SEO foundations: crawlable facets, clean canonicals, schema
- Planned a content calendar around buying-intent queries

## Services involved

Shopify Development, Shopify SEO, Technical SEO, Content Writing.

## Why it is here

This page exists so the agent can demonstrate navigating a visitor to a
relevant case study. The engagement is illustrative.
""",
    },
]

BLOG_POSTS = [
    {
        "slug": "what-local-seo-actually-changes",
        "title": "[SAMPLE] What Local SEO actually changes",
        "description": "Illustrative article on how local search visibility is built.",
        "service": "/seo/local-seo",
        "body": """
Local SEO is often described as "getting on the map". More precisely, it is
making a business legible to search engines as a real operation in a real
place.

## The three things that move local rankings

1. **Profile completeness** &mdash; hours, categories, service areas and photos
   on Google Business Profile.
2. **Consistency** &mdash; the same name, address and phone number everywhere
   the business is listed.
3. **Genuine local relevance** &mdash; pages that describe the actual area
   served, not a template with a city name swapped in.

## What it does not do

Local SEO does not compensate for a site that loads slowly or a profile with no
reviews. Those are prerequisites.
""",
    },
    {
        "slug": "when-to-invest-in-technical-seo",
        "title": "[SAMPLE] When to invest in Technical SEO",
        "description": "Illustrative article on prioritising technical SEO work.",
        "service": "/seo/technical-seo",
        "body": """
Technical SEO earns its place when crawlability, not content, is the constraint.

## Signs it is the bottleneck

- Pages exist but never appear in search results
- Search Console reports crawl or indexing errors at scale
- The site loads slowly on mobile connections
- Duplicate or near-duplicate URLs compete with each other

## Signs something else is the bottleneck

If pages are indexed and simply rank poorly, the issue is usually relevance or
authority. A technical audit will not fix thin content.
""",
    },
]

TESTIMONIALS = [
    {
        "quote": "They explained the plan in language we understood, and the "
                 "monthly reporting actually told us what changed and why.",
        "attribution": "[SAMPLE] Operations lead, multi-branch retailer",
    },
    {
        "quote": "The storefront launched on schedule and search traffic was "
                 "part of the plan from day one rather than an afterthought.",
        "attribution": "[SAMPLE] Founder, direct-to-consumer brand",
    },
    {
        "quote": "Responsive team. When priorities shifted mid-quarter they "
                 "reworked the roadmap without drama.",
        "attribution": "[SAMPLE] Marketing manager, B2B services firm",
    },
]
