# Product profile template — how to write a rich catalog entry

> Reference for whoever adds/edits product knowledge (via the dashboard's
> "paste specifications" flow, or directly in `data/knowledge_base.md`). This
> is a content template, not code — copy the section shape below for each
> product you want the agent to reason about deeply, not just quote a price
> for. Existing sparse entries (name/brand/price rows) keep working exactly
> as they do today; this is an option to go deeper on products worth it, not
> a required rewrite of everything at once.

## Why this shape

The agent's prompt (`app/prompts.py`) already tells it to never invent a
spec, never recite this content verbatim, and to answer customer objections
in its own words drawing on facts — not a script. That instruction is only
useful if the *facts* it can draw from actually exist in the retrieved
context. Today's price-table rows answer "what does it cost" well and
"why should I buy this over the competitor" not at all — the agent either
declines to answer (correct, per the never-invent rule) or drifts into
plausible-sounding invention (the failure mode that rule exists to prevent).
Each section below exists to close one specific gap like that.

## Section shape

Use one `###` sub-heading per product (matches the existing chunking
convention in `app/documents.py`/`app/rag.py` — a chunk is one `###`
section, so keep each product self-contained rather than spanning two).
Fill in whichever of these sections you actually have real information
for — an empty section is worse than a missing one, since the agent is
instructed to only use what's actually here.

```
### <Product name and model code>

**What it does:** one or two sentences, plain language, what job this
machine does on a site — not a marketing tagline.

**Who should buy it:** the project types, company sizes, or use cases this
genuinely fits well. Specific enough that the agent can match it to a
customer's stated situation, not generic ("construction companies").

**Who should NOT buy it:** the honest mismatch cases — too much machine for
a one-off job, wrong precision class for the stated use, a cheaper/simpler
option exists that fits better. This is what lets the agent recommend
AGAINST a sale when that's the right call, which reads as trustworthy, not
as talking someone out of buying.

**Features:** the real technical specs — dimensions, capacity, accuracy,
power source, whatever is actually distinguishing. Bullet list is fine here;
this section is reference data, not something recited verbatim in a reply.

**Benefits:** the outcome each feature produces for the customer, in plain
terms — not a restatement of the feature. "IP66-rated" is a feature;
"keeps working through monsoon site conditions without downtime" is the
benefit.

**Price:** approximate price range, same convention as the rest of the
catalog — always "approximately" / "starting from", sales team confirms
exact figures.

**Competitors:** which other brands/models a customer is likely comparing
this against.

**Advantages:** where this product genuinely wins against those
competitors — real differentiators, not generic superiority claims.

**Limitations:** the honest downsides versus competitors or versus a
customer's likely expectations. Skipping this section entirely reads as
overselling; a real sales engineer names the tradeoff before the customer
finds it themselves.

**Common objections:** the actual pushback customers give on this product —
price, an unfamiliar brand, a specific competitor's claim, a doubt about
durability or support.

**Responses:** the real, factual answer to each objection above — written
as material for the agent to draw from and rephrase, not as a script. A
canned "I understand your concern, but..." here will get recited close to
verbatim if written that way; write it as the underlying fact/argument
instead ("in-house Sokkia calibration center means no 2-week wait for
service, unlike importing calibration from [competitor's arrangement]") so
the agent has something to reformulate rather than something to quote.

**Frequently asked questions:** real Q&A pairs customers actually ask,
written as facts to draw from for the same reason as above.

**Upselling opportunities:** the accessory, higher-tier model, or paired
product that's genuinely relevant once a customer has committed to this
one — mirrors how `app/prompts.py` already asks the agent to mention one
relevant accessory once a machine is identified, never a full list.
```

## Worked example — Sokkia iM-55 Total Station

The existing price-table row (`### 3.1 Survey Equipment — Total Stations`
in `data/knowledge_base.md`) already has this product at ₹2,89,000/pc. Below
is what a fully deepened entry looks like for the same machine — this is a
sample to show the shape filled in with plausible, representative content,
not verified real specs; replace with the real machine's actual data before
using it to answer customers (the never-invent rule applies to this file's
own content once it's in the catalog, same as anything else retrieved).

```
### Sokkia iM-55 Total Station

**What it does:** measures distance and angle for land surveying and
construction layout — staking out boundaries, checking levels, mapping a
site before or during a build.

**Who should buy it:** land surveyors and construction contractors doing
routine layout and measurement work — building sites, road alignment
checks, boundary surveys — where solid accuracy at a working price matters
more than robotic/automated features.

**Who should NOT buy it:** a one-person crew needing to operate the
instrument alone from the prism end without a second person at the
total station — that needs the robotic iX series instead. Also not the
right fit for someone who only needs occasional, basic leveling — an auto
level is cheaper and simpler for that.

**Features:** reflectorless measurement up to ~500m, onboard data
recording, dual-side keyboard and display, IP66 dust/water rating,
long-life battery.

**Benefits:** reflectorless mode means one person can measure hard-to-reach
points without a prism assistant; the dual-side display means either
person at the instrument can read results without walking around it;
IP66 keeps it working through dusty or wet site conditions without special
handling.

**Price:** approximately ₹2,89,000 per unit.

**Competitors:** Topcon GM/ES series, Nikon Nivo series, and other total
stations in the same working-instrument class.

**Advantages:** Sokkia's own in-house calibration and repair center (the
only Japanese-Collimator-equipped one in Central East India per Divine
Empire's own service differentiators) means faster local service turnaround
than brands without a comparable local support setup.

**Limitations:** non-robotic — needs two people to operate at full
efficiency (one at the instrument, one at the prism), unlike the iX series.
Reflectorless range is solid but not class-leading versus the most premium
competitor models.

**Common objections:** "yeh mehenga hai" (this is expensive) compared to a
generic/unbranded total station; "kya isko akela chala sakte hain" (can one
person operate it alone).

**Responses:** the price difference versus an unbranded unit reflects
Sokkia's build quality and, more concretely, the in-house calibration and
repair support available locally — an unbranded unit's service issue often
means a long wait for parts or calibration from outside the region. On
single-operator use: the iM-55 itself needs two people for full efficiency;
if working alone is the actual requirement, the iX robotic series is the
correct recommendation instead of this one.

**Frequently asked questions:** Does it come with a tripod and prism? No,
those are sold separately (see Survey Accessories). Is calibration
included? No — calibration service is available separately, ₹14,500–16,500
per unit, at Divine Empire's own Sokkia-authorised center.

**Upselling opportunities:** aluminum telescopic tripod stand, survey
prism pole, and a spare Sokkia BDC70 battery are the natural pairing for a
first-time buyer setting up a complete survey kit.
```
