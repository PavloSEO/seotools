# Third-party notices

SEOHEAD Tools is MIT-licensed source code. The project also ships a vocabulary dataset and uses
ideas or public specifications from the sources below. Their original licences remain in force.

## Bundled vocabulary data

`seohead/data/schemaorg.json` is derived from the Schema.org vocabulary. Schema.org schemas are
provided under the **Creative Commons Attribution-ShareAlike 3.0** licence.

- Source: <https://schema.org/version/latest/schemaorg-current-https.jsonld>
- Terms: <https://schema.org/docs/terms.html>
- Licence: <https://creativecommons.org/licenses/by-sa/3.0/>

Changes in this repository are limited to packaging and loading the vocabulary for validation.
The dataset is not relicensed under MIT.

## Algorithm and interoperability references

The Python implementation is maintained in this repository. The following compatible projects
informed specific interoperability cases or algorithm design. They are not runtime dependencies
and their original source trees are not vendored here.

- **broken-link-checker** by Steven Vachon — MIT. Reference for the taxonomy of HTML elements and
  attributes that may carry URLs and for link-check failure categories.
  <https://github.com/stevenvachon/broken-link-checker>
- **seo** by Ian Nuttall — Apache-2.0. Reference for local-first CLI/MCP ergonomics, structured-data
  context handling, and evidence-first reporting.
  <https://github.com/iannuttall/seo>
- **FreeCrawl SEO Tool** by Kemal Acar — MIT. Reference for access-log aggregation,
  raw-versus-rendered comparisons, and scalable near-duplicate analysis.
  <https://github.com/kemalai/FreeCrawl-SEO-Tool>
- **structured-data-testing-tool** by Iain Collins — ISC. Reference for separating Schema.org
  validity from vendor-specific rich-result expectations.
  <https://github.com/iaincollins/structured-data-testing-tool>
- **seo-graph** by Joost de Valk and contributors — MIT. Reference for connected JSON-LD graph
  validation and dangling `@id` detection.
  <https://github.com/jdevalk/seo-graph>
- **orangeo-ai-visibility-skill** by OranAi Ltd — MIT. Reference for classifying AI crawler access
  and reviewing `llms.txt` structure.
  <https://github.com/OranAi-Ltd/orangeo-ai-visibility-skill>

Only compatible-licensed source or independently implemented ideas and public facts are used.
Repositories without a licence were treated as idea-level research only; no line of their source
code or creative text is included.

## Python dependencies

Installed dependencies retain their own licences. The resolved environment uses common
permissive or weak-copyleft terms, including MIT, BSD, Apache-2.0, ISC, PSF, MPL-2.0, and similar
licences. Build and documentation tooling can have additional dual-licensed components. Use your
package manager's licence report for the exact versions resolved in your environment, or generate
the per-profile dependency/license inventory described in
[docs/SOFTWARE_REVIEW.md](docs/SOFTWARE_REVIEW.md) — this file records bundled-data and
interoperability notices and is not a dependency inventory.
