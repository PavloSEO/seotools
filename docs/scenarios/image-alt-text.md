# Scenario 25 — Image alt text: missing, empty, and the images no crawler sees

## The question

> Accessibility flagged our images. How many are missing alt text, and are we sure that list is
> all of them?

The second half of that question is the interesting one. An alt-text list built from `<img>`
elements omits every image the page loads through CSS, and those are usually the largest ones on
the page.

## Covers

- **Images** — Missing Alt Text · Missing Alt Attribute · Alt Text Over 100 Characters · Background Images

## The chain

**1. A native crawl now carries its own per-`<img>` inventory.**

```bash
seohead crawl-site --url https://example.com --out-dir ./run --max-urls 200
```

Two checks run straight from that crawl's own evidence, no Screaming Frog export needed:
`IMG_MISSING_ALT_ATTRIBUTE` fires per page when at least one `<img>` has **no `alt` attribute at
all** — not even `alt=""` — and `IMG_ALT_TOO_LONG` fires when the longest alt string on the page
exceeds the configured threshold (100 characters by default, `thresholds.alt_max_chars`). A
decorative image marked up correctly with `alt=""` is *not* missing the attribute, and does not
fire either check: the attribute has the attribute; it is simply, correctly, empty.

`IMG_MISSING_ALT` (the "missing alt **text**" issue, which reads an absent attribute and an
empty one *together*) still names itself absent when no export supplies it:

```
{"id": "IMG_MISSING_ALT", "reason": "missing export: images_missing_alt"}
```

That is not "no problems" — it means the combined, coarser finding still needs an export; the
two finer-grained native checks above already ran.

**2. Get the combined alt-text list, and per-image detail, from a Screaming Frog export.**

```bash
seohead sf run --exports-dir ./exports --out report --tasks
```

`IMG_MISSING_ALT` activates when the `Images:Missing Alt Text` filter is present in the export
directory — the loader matches it on the filename token `missing_alt`, so any reasonable export
name works. This is still the only source for a per-image list (which URL, which image); the
native checks above report at the page level (how many images, on this page, are missing the
attribute or run long) because a native crawl's evidence is one row per page, not one row per
image.

**3. Reconcile the two.** On a native crawl, `IMG_MISSING_ALT_ATTRIBUTE` already separates the
one distinction that matters most — attribute absent versus deliberately empty. Add the SF
export when the deliverable needs the individual image URLs, or when the site also needs
"missing alt text" in SF's own combined sense (an empty alt included) rather than just an
absent attribute.

**4. Find the images that are in no `<img>` element at all.**

```bash
seohead parse --input '{"url": "https://example.com/page", "options": {"url_sources": true}}'
```

`url_sources` collects every URL-bearing carrier beyond `a[href]` — `img[src]`, `srcset`,
`source`, `video[poster]` — and the parser separately extracts `url()` references out of CSS
text. It is deliberately not limited to `background-image`: `border-image`, `list-style-image`,
`mask-image` and `content` all fetch a resource the same way, and a checker that knew only one
property would under-report. On a live site this found four images that were invisible to every
`<img>`-based inventory.

A CSS background has **no alt attribute to be missing**. That is the point: it carries no
accessible name at all, and if it conveys meaning the fix is markup, not an attribute.

**5. Put both lists in one task.**

```bash
seohead report-build --audit ./run/audit.json --format docx --out ./image-alt.docx
```

## What comes out

From `parse`, the shape of the carrier list — each URL with the tag and attribute it was found
on, so a developer knows where to look:

```json
{
  "url_sources": [
    {"url": "https://example.com/img/hero.jpg", "tag": "img", "attr": "src"},
    {"url": "https://example.com/img/panel-bg.jpg", "tag": "style", "attr": "css"},
    {"url": "https://example.com/img/tile.png", "tag": "div", "attr": "style"}
  ]
}
```

`attr: "css"` is a `url()` inside a `<style>` block; `attr: "style"` is one in an element's
inline `style` attribute. Those two values are exactly the set that identifies a background
image, because every other carrier is either an `<img>` or not an image at all.

And the deliverable is two lists with two different fixes: `<img>` elements that need alt text
written, and CSS backgrounds that need to become real images before alt text is even possible.

## What it costs

One request per page for `parse`. The alt-text inventory rides along with a Screaming Frog export
you already made. Nothing paid.

## What it cannot answer

- **Which specific image, on a native crawl.** `IMG_MISSING_ALT_ATTRIBUTE`/`IMG_ALT_TOO_LONG`
  report a per-page count, not a per-image URL — that granularity still needs the SF export in
  step 2.
- **Whether the alt text that exists is any good.** "image1.jpg" as alt text passes every length
  check here; it is not empty and it is not too long.
- **Images injected by JavaScript.** The parse reads served HTML and CSS text. A gallery built
  client-side needs the [rendering scenario](rendering.md) first.
- **Backgrounds declared in a linked stylesheet.** `parse` reads one document and performs no
  I/O of its own, so `url()` inside inline styles and `<style>` blocks is found and a `.css`
  file is reported as a resource whose contents were never opened.
- **Anything about image weight.** That is a separate chain, and it ends in re-encoded files
  rather than a list — see the [images scenario](images.md).
