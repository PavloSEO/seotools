"""Detect website technologies from HTML, response headers, and cookies.

The detector identifies CMSs, frameworks, servers, analytics, advertising pixels,
widgets, fonts, and third-party CDN libraries. In an SEO audit, this answers what
can be changed on the site, how traffic is measured, and how much third-party
JavaScript the page depends on.

Fingerprints are declared in :data:`SIGNATURES`: adding a technology means adding
a data row rather than changing matching logic. Every match includes the marker
that triggered it so findings remain inspectable and reproducible.

:func:`detect_tech` fetches a page and hands it to :func:`analyze_tech`, the pure
half that a crawl can call once per already-downloaded page without a second
request. :func:`tag_coverage` aggregates those per-page results into a site-wide
report: which templates carry which tag, whether two pages disagree on the id,
and how the underlying pages were measured (static markup vs. a rendered DOM).
"""

# ruff: noqa: RUF001
# Cyrillic strings in SIGNATURES are canonical Runet product labels, not prose.

from __future__ import annotations

import re
from typing import Any

from seohead.recon.net import http_client, normalize_url

# (category, name, search location, marker)
#   header  — response-header name;
#   value   — substring in any response-header value;
#   cookie  — cookie name;
#   html    — case-insensitive substring in the response body;
#   script  — substring in the ``src`` of an included script.
SIGNATURES: tuple[tuple[str, str, str, str], ...] = (
    # --- CMSs and site builders ---
    ("cms", "WordPress", "html", "/wp-content/"),
    ("cms", "WordPress", "html", "/wp-includes/"),
    ("cms", "WordPress", "html", "/wp-json/"),
    ("cms", "1С-Битрикс", "html", "/bitrix/"),
    ("cms", "1С-Битрикс", "cookie", "BITRIX_SM_GUEST_ID"),
    ("cms", "OpenCart", "html", "catalog/view/theme"),
    ("cms", "OpenCart", "html", "index.php?route="),
    ("cms", "Drupal", "html", "/sites/default/files"),
    ("cms", "Drupal", "header", "x-drupal-cache"),
    ("cms", "Joomla", "html", "/media/jui/"),
    ("cms", "MODX", "html", "modx"),
    ("cms", "Tilda", "html", "tildacdn.com"),
    ("cms", "Wix", "html", "static.parastorage.com"),
    ("cms", "Squarespace", "html", "static1.squarespace.com"),
    ("cms", "Ghost", "html", "content/themes/casper"),
    ("cms", "Webflow", "html", "assets.website-files.com"),
    ("cms", "HubSpot CMS", "html", "hs-scripts.com"),
    ("cms", "Directus", "html", "/assets/directus"),
    # --- E-commerce platforms ---
    ("ecommerce", "Shopify", "html", "cdn.shopify.com"),
    ("ecommerce", "Shopify", "header", "x-shopify-stage"),
    ("ecommerce", "WooCommerce", "html", "/plugins/woocommerce/"),
    ("ecommerce", "Magento", "html", "/static/version"),
    ("ecommerce", "PrestaShop", "cookie", "PrestaShop-"),
    ("ecommerce", "InSales", "html", "assets.insales.ru"),
    # --- Frameworks and rendering ---
    ("framework", "Next.js", "html", "/_next/static"),
    ("framework", "Next.js", "header", "x-nextjs-cache"),
    ("framework", "Nuxt", "html", "/_nuxt/"),
    ("framework", "SvelteKit", "html", "/_app/immutable"),
    ("framework", "Astro", "html", "astro-island"),
    ("framework", "Gatsby", "html", "/page-data/"),
    ("framework", "Remix", "html", "__remixContext"),
    ("framework", "Angular", "html", "ng-version="),
    ("framework", "Vue.js", "html", "data-v-app"),
    ("framework", "React", "html", "data-reactroot"),
    ("library", "jQuery", "script", "jquery"),
    # --- Servers and runtimes ---
    ("server", "nginx", "value", "nginx"),
    ("server", "Apache", "value", "apache"),
    ("server", "LiteSpeed", "value", "litespeed"),
    ("server", "Microsoft IIS", "value", "microsoft-iis"),
    ("server", "Caddy", "value", "caddy"),
    # Match the value, not merely the header's presence: Next.js, Express, and
    # ASP.NET all emit ``x-powered-by``. Treating the header itself as a PHP
    # fingerprint caused a false PHP detection on a production Next.js site.
    ("runtime", "PHP", "value", "php/"),
    ("runtime", "PHP", "value", "php "),
    ("runtime", "Express", "value", "express"),
    ("runtime", "ASP.NET", "header", "x-aspnet-version"),
    ("runtime", "Phusion Passenger", "value", "passenger"),
    # --- Analytics and counters ---
    ("analytics", "Google Analytics 4", "html", "googletagmanager.com/gtag/js"),
    ("analytics", "Google Tag Manager", "html", "googletagmanager.com/gtm.js"),
    ("analytics", "Яндекс.Метрика", "html", "mc.yandex.ru/metrika"),
    ("analytics", "Яндекс.Метрика", "html", "ym("),
    ("analytics", "Matomo", "html", "matomo.js"),
    ("analytics", "Plausible", "script", "plausible.io/js"),
    ("analytics", "Fathom", "script", "cdn.usefathom.com"),
    ("analytics", "Hotjar", "html", "static.hotjar.com"),
    ("analytics", "Clarity", "html", "clarity.ms/tag"),
    ("analytics", "Vercel Analytics", "script", "/_vercel/insights"),
    ("analytics", "Amplitude", "script", "amplitude"),
    ("analytics", "Mixpanel", "script", "mixpanel"),
    # --- Advertising pixels ---
    ("pixel", "Meta Pixel", "html", "connect.facebook.net"),
    ("pixel", "TikTok Pixel", "html", "analytics.tiktok.com"),
    ("pixel", "LinkedIn Insight", "html", "snap.licdn.com"),
    ("pixel", "Google Ads", "html", "googleadservices.com"),
    ("pixel", "VK Pixel", "html", "vk.com/js/api/openapi"),
    ("pixel", "Top.Mail.Ru", "html", "top-fwz1.mail.ru"),
    ("pixel", "Reddit Pixel", "html", "redditstatic.com/ads"),
    # --- Widgets and support ---
    ("widget", "Intercom", "html", "widget.intercom.io"),
    ("widget", "Crisp", "html", "client.crisp.chat"),
    ("widget", "Tawk.to", "html", "embed.tawk.to"),
    ("widget", "Jivo", "html", "code.jivo"),
    ("widget", "Zendesk", "html", "static.zdassets.com"),
    ("widget", "Bitrix24 CRM", "html", "cdn-ru.bitrix24.ru"),
    ("widget", "Calendly", "script", "assets.calendly.com"),
    # --- Consent and privacy ---
    ("consent", "Cookiebot", "script", "consent.cookiebot.com"),
    ("consent", "OneTrust", "script", "cdn.cookielaw.org"),
    ("consent", "Osano", "script", "cmp.osano.com"),
    # --- Fonts and third-party CDNs ---
    ("fonts", "Google Fonts", "html", "fonts.googleapis.com"),
    ("fonts", "Adobe Fonts", "html", "use.typekit.net"),
    ("cdn-lib", "jsDelivr", "script", "cdn.jsdelivr.net"),
    ("cdn-lib", "cdnjs", "script", "cdnjs.cloudflare.com"),
    ("cdn-lib", "unpkg", "script", "unpkg.com"),
    # --- Protection ---
    ("protection", "Cloudflare Turnstile", "html", "challenges.cloudflare.com"),
    ("protection", "Google reCAPTCHA", "html", "google.com/recaptcha"),
    ("protection", "hCaptcha", "html", "hcaptcha.com"),
    ("protection", "Cloudflare", "header", "cf-ray"),
    ("protection", "DDoS-Guard", "header", "ddos-guard"),
    ("protection", "Qrator", "value", "qrator"),
    ("protection", "Sucuri", "value", "sucuri"),
    ("protection", "Imperva Incapsula", "cookie", "incap_ses"),
    ("protection", "ModSecurity", "header", "mod_security"),
    # --- CMS / SSG / headless ---
    ("cms", "Craft CMS", "cookie", "craft_session"),
    ("cms", "Statamic", "html", "statamic"),
    ("cms", "Sanity", "script", "cdn.sanity.io"),
    ("cms", "Contentful", "script", "ctfassets.net"),
    ("cms", "Payload CMS", "script", "payloadcms"),
    ("cms", "Wagtail", "html", "wagtail"),
    ("cms", "October CMS", "html", "/modules/system/"),
    ("cms", "Umbraco", "cookie", "umbrac"),
    ("cms", "uCoz", "html", "ucoz."),
    ("cms", "Moguta.CMS", "html", "mg-base"),
    ("cms", "Hugo", "html", "hugo-generator"),
    ("cms", "Jekyll", "html", 'name="generator" content="jekyll'),
    ("cms", "Eleventy", "html", "eleventy"),
    ("cms", "Docusaurus", "html", "docusaurus"),
    ("cms", "VitePress", "html", "vitepress"),
    ("cms", "MkDocs", "html", "mkdocs"),
    ("cms", "Sphinx", "html", "sphinx"),
    # --- E-commerce ---
    ("ecommerce", "BigCommerce", "html", "bigcommerce.com"),
    ("ecommerce", "CS-Cart", "html", "/var/cache/templates/cs"),
    ("ecommerce", "Saleor", "script", "saleor"),
    ("ecommerce", "Spree", "html", "spree"),
    ("ecommerce", "Magento", "header", "x-magento-cache"),
    ("ecommerce", "VirtueMart", "html", "/components/com_virtuemart/"),
    # --- Back-end web frameworks ---
    ("framework", "Laravel", "cookie", "laravel_session"),
    ("framework", "Django", "cookie", "csrftoken"),
    ("framework", "Ruby on Rails", "cookie", "_rails_session"),
    ("framework", "Flask", "value", "werkzeug"),
    ("framework", "Symfony", "cookie", "symfony"),
    ("framework", "CodeIgniter", "cookie", "ci_session"),
    ("framework", "Yii", "cookie", "yii"),
    ("framework", "Spring", "header", "x-application-context"),
    ("framework", "Qwik", "html", "qwikify"),
    ("framework", "Preact", "html", "data-preactroot"),
    ("framework", "Alpine.js", "html", "x-data"),
    ("framework", "htmx", "html", "hx-get"),
    ("framework", "Turbo (Hotwire)", "html", "data-turbo"),
    ("framework", "Stimulus", "html", "data-controller"),
    # --- UI and CSS frameworks ---
    ("library", "Tailwind CSS", "html", "tailwind"),
    ("library", "Bootstrap", "html", "bootstrap"),
    ("library", "Bulma", "html", "bulma"),
    ("library", "Material-UI", "html", "mui-"),
    # Do not use ``ant-``: it matched ``font-variant-numeric`` in Tailwind output
    # during a production audit.
    ("library", "Ant Design", "html", "ant-design"),
    ("library", "Ant Design", "html", "antd."),
    ("library", "shadcn/ui", "html", "data-radix"),
    # --- JavaScript libraries ---
    ("library", "Lodash", "script", "lodash"),
    ("library", "Moment.js", "script", "moment.js"),
    ("library", "Day.js", "script", "day.js"),
    ("library", "D3.js", "script", "d3.js"),
    ("library", "Three.js", "script", "three.js"),
    ("library", "GSAP", "script", "gsap.js"),
    ("library", "Swiper", "script", "swiper"),
    ("library", "Slick Slider", "script", "slick.js"),
    ("library", "AOS", "script", "aos.js"),
    ("library", "Lottie", "script", "lottie"),
    # --- Bundlers and transpilers ---
    ("library", "webpack", "html", "/static/js/"),
    ("library", "Vite", "html", "vite"),
    ("library", "esbuild", "html", "esbuild"),
    ("library", "Parcel", "html", "parcel"),
    ("library", "Rollup", "html", "rollup"),
    ("library", "Babel", "script", "babel"),
    # --- Runtimes and application servers ---
    ("runtime", "Deno", "value", "deno"),
    ("runtime", "Gunicorn", "value", "gunicorn"),
    ("runtime", "uWSGI", "value", "uwsgi"),
    ("runtime", "Puma", "value", "puma"),
    ("runtime", "Tomcat", "value", "coyote"),
    # --- Additional analytics ---
    ("analytics", "PostHog", "script", "posthog"),
    ("analytics", "Heap", "script", "heap.js"),
    ("analytics", "FullStory", "script", "fullstory"),
    ("analytics", "Adobe Analytics", "script", "omtrdc.net"),
    ("analytics", "Statcounter", "script", "statcounter.com/counter/"),
    ("analytics", "Openstat", "html", "openstat.ru/counter"),
    ("analytics", "LiveInternet", "html", "liveinternet.ru/click"),
    ("analytics", "Clicky", "script", "static.getclicky.net"),
    ("analytics", "GoatCounter", "script", "goatcounter.com/count"),
    ("analytics", "Chartbeat", "script", "chartbeat.js"),
    # --- Additional advertising and pixels ---
    ("pixel", "Pinterest Tag", "html", "s.pinimg.com/ct/core.js"),
    ("pixel", "Quora Pixel", "html", "q.quora.com/qevents"),
    ("pixel", "Bing UET", "html", "bat.bing.com/bat.js"),
    ("pixel", "Yandex.Direct", "html", "an.yandex.ru/"),
    ("pixel", "Outbrain", "html", "outbrain.js"),
    ("pixel", "Taboola", "html", "taboola.com/libtrc/"),
    # --- Additional chats and widgets ---
    ("widget", "Drift", "script", "js.driftt.com"),
    ("widget", "LiveChat", "script", "livechatinc.com/tracking.js"),
    ("widget", "Userlike", "script", "userlike.com"),
    ("widget", "HelpCrunch", "script", "helpcrunch.com"),
    ("widget", "Verbox", "html", "verbox.ru"),
    ("widget", "Typeform", "script", "embed.typeform.com"),
    ("widget", "Youtube subscribe", "script", "youtube.com/iframe_api"),
    # --- Additional consent platforms ---
    ("consent", "iubenda", "script", "iubenda.com/cookie"),
    ("consent", "Didomi", "script", "didomi.io"),
    ("consent", "Usercentrics", "script", "usercentrics.eu"),
    ("consent", "Termly", "script", "app.termly.io"),
    ("consent", "Klaro", "script", "klaro.js"),
    # --- Payment systems ---
    ("payment", "Stripe", "script", "js.stripe.com"),
    ("payment", "PayPal", "script", "paypal.com/sdk/js"),
    ("payment", "YooKassa", "script", "yookassa.ru/checkout"),
    ("payment", "CloudPayments", "script", "cloudpayments.ru/scripts/"),
    ("payment", "Tinkoff Pay", "script", "securepay.tinkoff.ru"),
    ("payment", "Sber Pay", "html", "securepayments.sberbank.ru"),
    ("payment", "Robokassa", "script", "auth.robokassa.ru"),
    ("payment", "LiqPay", "script", "static.liqpay.ua/libjs"),
    ("payment", "Braintree", "script", "js.braintreegateway.com"),
    # --- CDN and hosting providers ---
    ("cdn-lib", "Bunny CDN", "value", "bunnycdn"),
    ("cdn-lib", "Fastly", "header", "x-served-by"),
    ("cdn-lib", "AWS CloudFront", "header", "x-amz-cf-id"),
    ("cdn-lib", "Vercel", "value", "vercel"),
    ("cdn-lib", "Netlify", "header", "x-netlify"),
    ("cdn-lib", "GitHub Pages", "value", "github.com"),
    ("cdn-lib", "Amazon S3", "header", "x-amz-request-id"),
    # --- Email marketing and CRM ---
    ("marketing", "Mailchimp", "script", "mc.us.list-manage.com"),
    ("marketing", "HubSpot", "html", "js.hs-scripts.com"),
    ("marketing", "Unisender", "script", "unisender.com"),
    ("marketing", "Mindbox", "script", "mindbox.ru"),
    ("marketing", "SendGrid", "header", "x-sendgrid"),
    # --- Video players ---
    ("video", "Vimeo", "html", "player.vimeo.com"),
    ("video", "YouTube embed", "html", "youtube.com/embed/"),
    ("video", "JWPlayer", "script", "jwplayer.js"),
    ("video", "Video.js", "script", "video.js"),
    ("video", "Video.js", "html", "video-js"),
    ("video", "Plyr", "script", "plyr.js"),
    ("video", "Plyr", "html", "plyr__"),
    ("video", "Brightcove", "script", "players.brightcove.net"),
    ("video", "Wistia", "script", "fast.wistia.com"),
    ("video", "Kaltura", "script", "kaltura"),
    ("video", "HLS.js", "script", "hls.js"),
    ("video", "Flowplayer", "script", "flowplayer"),
    # --- Site search ---
    ("search", "Algolia", "script", "algoliasearch"),
    ("search", "Algolia", "script", "instantsearch.js"),
    ("search", "Swiftype", "script", "swiftype.com"),
    ("search", "Searchanise", "script", "searchanise.io"),
    ("search", "AddSearch", "script", "addsearch.com"),
    ("search", "Coveo", "script", "coveo.com"),
    ("search", "Yext", "script", "yextstatic.com"),
    # --- A/B testing and personalization ---
    ("personalization", "Optimizely", "script", "optimizely.com"),
    ("personalization", "VWO", "script", "visualwebsiteoptimizer.com"),
    ("personalization", "Convert.com", "script", "convert.com"),
    ("personalization", "AB Tasty", "script", "abtasty.com"),
    ("personalization", "Dynamic Yield", "script", "dynamicyield.com"),
    ("personalization", "Kameleoon", "script", "kameleoon.eu"),
    ("personalization", "Monetate", "script", "monetate.net"),
    # --- Maps ---
    ("maps", "Google Maps", "script", "maps.googleapis.com/maps/api/js"),
    ("maps", "Google Maps", "html", "maps.google.com/maps"),
    ("maps", "Яндекс.Карты", "script", "api-maps.yandex.ru"),
    ("maps", "Leaflet", "script", "leaflet.js"),
    ("maps", "Mapbox", "script", "api.mapbox.com/mapbox-gl"),
    ("maps", "Mapbox", "html", "mapboxgl-canvas-container"),
    ("maps", "2GIS", "script", "maps.api.2gis.ru"),
    # --- Error tracking and front-end monitoring ---
    ("monitoring", "Sentry", "script", "sentry-cdn"),
    ("monitoring", "Sentry", "html", "sentry.init"),
    ("monitoring", "Rollbar", "script", "rollbar"),
    ("monitoring", "Bugsnag", "script", "bugsnag"),
    ("monitoring", "Datadog RUM", "script", "datadoghq-browser-agent"),
    ("monitoring", "Raygun", "script", "raygun"),
    ("monitoring", "LogRocket", "script", "logrocket"),
    # --- CRM and sales ---
    ("crm", "amoCRM", "html", "amocrm"),
    ("crm", "amoCRM", "html", "amoforms"),
    ("crm", "Pipedrive", "html", "pipedrive"),
    ("crm", "Salesforce", "html", "force.com"),
    # --- Email marketing and push notifications ---
    ("marketing", "MailerLite", "script", "mailerlite.com"),
    ("marketing", "MailerLite", "html", "mailerlite-webform"),
    ("marketing", "GetResponse", "script", "getresponse.com"),
    ("marketing", "SendPulse", "script", "sendpulse.com"),
    ("marketing", "ConvertKit", "script", "convertkit.com"),
    ("marketing", "Brevo", "script", "sibforms.com"),
    ("marketing", "ActiveCampaign", "script", "activecampaign.com"),
    ("marketing", "AWeber", "script", "aweber.com"),
    ("marketing", "Klaviyo", "script", "klaviyo.com"),
    ("marketing", "OneSignal", "script", "onesignal.com"),
    ("marketing", "Pushwoosh", "script", "cdn.pushwoosh.com"),
    # --- Accessibility ---
    ("a11y", "UserWay", "script", "userway.org"),
    ("a11y", "AudioEye", "script", "audioeye.com"),
    ("a11y", "AccessiBe", "script", "accessibe.com"),
    # --- Page translation ---
    ("translation", "Weglot", "script", "weglot.com"),
    ("translation", "ConveyThis", "script", "conveythis.com"),
    ("translation", "LangShop", "script", "langshop.com"),
    # --- Forms ---
    ("forms", "Gravity Forms", "html", "gravityforms"),
    ("forms", "WPForms", "html", "wpforms"),
    ("forms", "Formspree", "html", "formspree.io"),
    ("forms", "Tally", "script", "tally.so"),
    ("forms", "Jotform", "script", "jotform.com"),
    ("forms", "Formstack", "script", "formstack.com"),
    ("forms", "Ninja Forms", "html", "ninja-forms"),
    ("forms", "Contact Form 7", "html", "contact-form-7"),
    # --- Native advertising and ad widgets ---
    ("ads", "Яндекс.Дзен", "html", "yandex.ru/zen"),
    ("ads", "Яндекс.Дзен", "script", "zen.yandex.ru"),
    ("ads", "Google AdSense", "html", "adsbygoogle"),
    ("ads", "AdFox", "script", "ads.adfox.ru"),
    ("ads", "Google Ad Manager", "script", "securepubads.g.doubleclick.net"),
    ("ads", "RTB House", "script", "creativecdn.com"),
    # --- Reviews and ratings ---
    ("reviews", "Trustpilot", "script", "trustpilot.com"),
    ("reviews", "Trustpilot", "html", "trustpilot-widget"),
    ("reviews", "Yotpo", "script", "yotpo.com"),
    ("reviews", "Cusrev", "script", "cusrev.com"),
    ("reviews", "Bazaarvoice", "script", "bazaarvoice.com"),
    ("reviews", "Reviews.io", "script", "reviews.io"),
    # --- Social sharing widgets ---
    ("share", "AddThis", "script", "addthis.com"),
    ("share", "ShareThis", "script", "sharethis.com"),
    ("share", "Яндекс.Поделиться", "html", "yastatic.net/share2"),
    ("share", "Shareaholic", "script", "shareaholic"),
    # --- Booking and online appointments ---
    ("booking", "Dikidi", "html", "dikidi.ru"),
    ("booking", "Yclients", "script", "w.yclients.com"),
    ("booking", "Bookform", "html", "bookform.ru"),
    ("booking", "EasyWeek", "script", "easyweek.io"),
    # --- Delivery and logistics ---
    ("logistics", "СДЭК", "script", "cdek.ru"),
    ("logistics", "СДЭК", "html", "cdek-widget"),
    ("logistics", "Boxberry", "html", "boxberry.ru"),
    ("logistics", "DPD", "script", "dpd.ru"),
    ("logistics", "Яндекс.Доставка", "script", "delivery.yandex.ru"),
    ("logistics", "Почта России", "script", "pochta.ru"),
    # --- Comment systems ---
    ("comments", "Disqus", "script", "disqus.com"),
    ("comments", "Hypercomments", "script", "hypercomments.com"),
    ("comments", "Cackle", "script", "cackle.me"),
    ("comments", "ВКонтакте Comments", "html", "vk.com/widget_comments"),
    # --- Additional chats and widgets ---
    ("widget", "MeTalk", "script", "me-talk.ru"),
    ("widget", "LiveTex", "script", "livetex.ru"),
    ("widget", "Carrot Quest", "script", "carrotquest.io"),
    ("widget", "Tidio", "script", "tidio.co"),
    ("widget", "Usedesk", "script", "usedesk.ru"),
    ("widget", "Webim", "script", "webim.ru"),
    # --- Additional security and WAF services ---
    ("protection", "Wordfence", "cookie", "wfvt_"),
    ("protection", "BitNinja", "header", "x-bitninja"),
    ("protection", "Akamai", "value", "akamai"),
    ("protection", "DataDome", "cookie", "datadome"),
    ("protection", "PerimeterX", "cookie", "_px"),
    ("protection", "Reblaze", "cookie", "rbzid"),
    ("protection", "Distil", "value", "distil"),
    # --- Additional headless and visual CMSs ---
    ("cms", "Storyblok", "script", "storyblok.com"),
    ("cms", "Storyblok", "html", "storyblok"),
    ("cms", "Builder.io", "script", "cdn.builder.io"),
    ("cms", "Builder.io", "html", "builder-components"),
    ("cms", "Prismic", "script", "prismic.io"),
    ("cms", "Hygraph", "html", "hygraph.com"),
    ("cms", "Kontent", "script", "deliver.kontent.ai"),
    ("cms", "Cosmic", "script", "cosmicjs"),
    ("cms", "ButterCMS", "script", "buttercms.com"),
    ("cms", "Decap CMS", "script", "decap"),
    # BaaS providers are visible through their client SDKs or service subdomains,
    # even when the underlying database is not directly exposed.
    ("baas", "Firebase", "script", "firebaseio.com"),
    ("baas", "Firebase", "script", "firebasejs"),
    ("baas", "Supabase", "script", "supabase.co"),
    ("baas", "Appwrite", "script", "appwrite"),
    ("baas", "Amplify (AWS)", "script", "aws-amplify"),
    # Search services visible through client SDKs. Self-hosted Elasticsearch and
    # Solr require network interception and cannot be inferred from static HTML.
    ("search", "Doofinder", "script", "doofinder.net"),
    ("search", "Bloomreach", "script", "bloomreach"),
    ("search", "Klevu", "script", "klevu"),
    # --- Additional payment systems ---
    ("payment", "Mollie", "script", "js.mollie.com"),
    ("payment", "Square", "script", "js.squareup.com"),
    ("payment", "Razorpay", "script", "checkout.razorpay.com"),
    # --- Runet-specific analytics and counters ---
    # Use concrete counter domains; ``kind=script`` avoids accidental matches in copy.
    ("analytics", "Rambler Top100", "script", "counter.rambler.ru/top100"),
    ("analytics", "HotLog", "script", "hotlog.ru"),
    ("analytics", "Gemius", "script", "gemius.pl"),
    ("analytics", "Mediascope (TNS)", "script", "tns-counter.ru"),
    ("analytics", "Weborama", "script", "weborama.fr"),
    ("analytics", "Calltouch", "script", "calltouch.ru"),
    ("analytics", "MTS Marketer", "script", "mts.ru/marketer"),
    # --- Runet-specific advertising, pixels, and personalization ---
    ("ads", "AdRiver", "script", "adriver.ru"),
    ("pixel", "VK Pixel", "html", "vk.com/rtrg"),  # VK retargeting pixel
    ("personalization", "Retail Rocket", "script", "retailrocket"),
    ("personalization", "Salesbeat", "script", "salesbeat"),
    ("marketing", "Convead", "script", "convead.io"),
    ("marketing", "VK Donut", "html", "vk.com/donut"),
    ("marketing", "TargetHunter", "script", "targethunter.net"),
    ("marketing", "DashaMail", "script", "dashamail.ru"),
    # --- Runet-specific forms, quizzes, and input widgets ---
    ("forms", "Marquiz", "script", "marquiz.ru"),
    ("forms", "Formdesigner", "script", "formdesigner.ru"),
    ("forms", "DaData", "script", "dadata.ru"),  # address-suggestion widget
    ("cms", "Flexbe", "script", "flexbe.com"),  # landing-page builder
    # --- Runet-specific CRM and sales ---
    ("crm", "RetailCRM", "script", "retailcrm.ru"),
    # --- Runet-specific logistics and delivery ---
    ("logistics", "Glavpunkt", "script", "glavpunkt.ru"),
    ("logistics", "СберЛогистика", "script", "sberlogistics.ru"),
    ("logistics", "ПЭК", "script", "pecom.ru"),
    ("logistics", "Деловые Линии", "script", "dellin.ru"),
    ("logistics", "5post", "script", "5post.ru"),
    # --- Runet-specific payment systems ---
    ("payment", "QIWI", "script", "qiwi.com"),
    ("payment", "PayAnyWay", "script", "payanyway.ru"),
    ("payment", "Единая касса (Wallet One)", "script", "walletone.com"),
    ("payment", "RBKmoney", "script", "rbkmoney.com"),
    ("payment", "Интеркасса", "script", "interkassa.com"),
    # ── Runet market ──────────────────────────────────────────────────────────
    # Fingerprints absent from general public databases, which focus primarily on
    # Western products. Russian platforms, payment gateways, call tracking, and
    # widgets are otherwise missing or reported as unknown. This layer is essential
    # for understanding a Runet site's ownership boundaries and editable systems.
    ("cms", "ocStore", "html", "ocstore"),
    ("cms", "UMI.CMS", "html", "umi.cms"),
    ("cms", "UMI.CMS", "html", "/templates/main/"),
    ("cms", "NetCat", "html", "/netcat_files/"),
    ("cms", "NetCat", "html", "/netcat/"),
    ("cms", "HostCMS", "html", "/hostcmsfiles/"),
    ("cms", "HostCMS", "html", "hostcms"),
    ("cms", "DIAFAN.CMS", "html", "/modules/diafan"),
    ("cms", "DIAFAN.CMS", "html", "diafan"),
    ("cms", "Webasyst", "html", "/wa-content/"),
    ("cms", "Webasyst", "html", "/wa-data/"),
    ("cms", "TYPO3", "html", "/typo3temp/"),
    ("cms", "MegaGroup", "html", "megagroup"),
    ("cms", "Nethouse", "html", "nethouse"),
    ("cms", "uKit", "html", "ukit.com"),
    ("cms", "Craftum", "html", "craftum"),
    ("cms", "LPgenerator", "html", "lpgenerator"),
    ("ecommerce", "AdvantShop", "html", "advantshop"),
    ("ecommerce", "Simpla", "html", "/design/simpla"),
    ("ecommerce", "Ecwid", "script", "ecwid.com"),
    ("framework", "Svelte", "html", "data-sveltekit"),
    ("framework", "Svelte", "html", "__sveltekit"),
    # Call tracking reveals how calls are attributed. Script-based number swapping
    # can also make the visible phone inconsistent with Organization structured data.
    ("marketing", "Roistat", "script", "roistat"),
    ("marketing", "CoMagic", "script", "comagic"),
    ("marketing", "Callibri", "script", "callibri"),
    ("marketing", "Mango Office", "script", "mango-office"),
    ("marketing", "Zadarma", "script", "zadarma"),
    ("marketing", "Flocktory", "script", "flocktory"),
    ("marketing", "Sendsay", "script", "sendsay"),
    # Chat and callback widgets add transfer weight and may cause layout shifts;
    # performance audits often expose them as previously unaccounted third-party code.
    ("widget", "Envybox", "script", "envybox"),
    ("widget", "Talk-Me", "script", "talk-me"),
    ("widget", "Chatra", "script", "chatra"),
    ("widget", "Битрикс24", "script", "bitrix24"),
    ("widget", "Битрикс24", "script", "b24"),
    # Payment gateways determine where checkout runs and whether the customer leaves
    # the site for a third-party domain at the final conversion step.
    ("payment", "ЮKassa", "script", "yookassa"),
    ("payment", "ЮKassa", "script", "yoomoney"),
    ("payment", "Тинькофф Касса", "script", "securepay.tinkoff"),
    ("payment", "Тинькофф Касса", "script", "acdn.tinkoff"),
    ("payment", "PayKeeper", "script", "paykeeper"),
    ("logistics", "PickPoint", "script", "pickpoint"),
)

# Frameworks that render the page themselves; co-detection with a CMS suggests headless.
SSR_FRAMEWORKS = frozenset({"Next.js", "Nuxt", "SvelteKit", "Astro", "Gatsby", "Remix"})

_GENERATOR_RE = re.compile(
    r"<meta[^>]+name=[\"']generator[\"'][^>]+content=[\"']([^\"']+)", re.IGNORECASE
)
_VERSION_RE = re.compile(r"(\d+\.\d+(?:\.\d+)?)")
MAX_HTML_BYTES = 3_000_000  # fingerprints occur early; cap memory on oversized pages

# A SIGNATURES marker proves a technology is present but not which deployment of
# it: two properties can both load Google Tag Manager while pointing at different
# containers. These patterns capture the id itself, so a site-wide report can tell
# "GTM is everywhere" apart from "two conflicting GTM containers are both live".
IDENTIFIER_PATTERNS: dict[str, re.Pattern[str]] = {
    "Google Analytics 4": re.compile(r"\b(G-[A-Z0-9]{6,10})\b"),
    "Google Tag Manager": re.compile(r"\b(GTM-[A-Z0-9]{4,8})\b"),
    "Яндекс.Метрика": re.compile(r"ym\(\s*(\d{5,9})\s*,\s*[\"']init[\"']"),
}


def _script_sources(html: str) -> list[str]:
    """Extract script ``src`` values with Beautiful Soup instead of regex."""
    try:
        from bs4 import BeautifulSoup
    except ImportError:  # pragma: no cover - Beautiful Soup is a core dependency
        return re.findall(r"<script[^>]+src=[\"']([^\"']+)", html, re.IGNORECASE)
    soup = BeautifulSoup(html, "html.parser")
    return [str(tag.get("src")) for tag in soup.find_all("script") if tag.get("src")]


def _match(
    kind: str,
    marker: str,
    *,
    html_low: str,
    headers: dict[str, str],
    cookies: dict[str, str],
    scripts_low: str,
) -> str | None:
    """Return human-readable evidence when a fingerprint matches."""
    low = marker.lower()
    if kind == "header" and low in headers:
        return f"header {marker}"
    if kind == "value":
        for name, value in headers.items():
            if low in value.lower():
                return f"{name}: {value[:60]}"
    if kind == "cookie":
        for name in cookies:
            if low in name.lower():
                return f"cookie {name}"
    if kind == "html" and low in html_low:
        return f"HTML: {marker}"
    if kind == "script" and low in scripts_low:
        return f"script: {marker}"
    return None


def detect_tech(url: str, timeout: float = 25.0) -> dict[str, Any]:
    """Fetch one page and detect technologies with a single request."""
    target = normalize_url(url)
    if not target:
        return {"ok": False, "error": f"Not a valid URL: {url!r}"}
    try:
        client, _ = http_client(timeout)
    except ImportError:
        return {"ok": False, "error": "httpx is required"}

    try:
        with client:
            resp = client.get(target)
            html = resp.text[:MAX_HTML_BYTES]
    # Network failures are returned as tool data rather than raised to the caller.
    except Exception as exc:
        return {"ok": False, "url": target, "error": str(exc)}

    return analyze_tech(
        html,
        headers=dict(resp.headers),
        cookies=dict(resp.cookies),
        url=target,
        final_url=str(resp.url),
        status_code=resp.status_code,
    )


def analyze_tech(
    html: str,
    *,
    headers: dict[str, str] | None = None,
    cookies: dict[str, str] | None = None,
    url: str = "",
    final_url: str | None = None,
    status_code: int | None = None,
    rendered: bool = False,
) -> dict[str, Any]:
    """Fingerprint an already-fetched document; makes zero network requests.

    Splitting this out of :func:`detect_tech` is what lets a crawl fingerprint
    every page it already downloaded instead of fetching each one a second time
    just for this check. ``rendered`` records whether ``html`` is the raw response
    body or a post-script DOM snapshot — several tags are visible in one and not
    the other, and :func:`tag_coverage` needs to know which it is looking at.
    """
    headers = {k.lower(): v for k, v in (headers or {}).items()}
    cookies = dict(cookies or {})
    html = html[:MAX_HTML_BYTES]
    page_url = final_url or url
    html_low = html.lower()
    scripts = _script_sources(html)
    scripts_low = " ".join(scripts).lower()

    found: dict[str, dict[str, Any]] = {}
    for category, name, kind, marker in SIGNATURES:
        hit = _match(
            kind,
            marker,
            html_low=html_low,
            headers=headers,
            cookies=cookies,
            scripts_low=scripts_low,
        )
        if hit and name not in found:
            found[name] = {"name": name, "category": category, "evidence": hit}

    generator = _GENERATOR_RE.search(html)
    if generator:
        label = generator.group(1).strip()
        found.setdefault(
            label, {"name": label, "category": "cms", "evidence": "meta name=generator"}
        )
        # The generator tag is the only source that commonly exposes a direct,
        # trustworthy CMS version.
        for entry in found.values():
            if entry["category"] == "cms" and entry["name"].lower() in label.lower():
                version = _VERSION_RE.search(label)
                if version:
                    entry["version"] = version.group(1)

    powered = headers.get("x-powered-by")
    if powered:
        # ``x-powered-by`` may include a version and is more precise than the generic
        # ``PHP`` fingerprint, so retain the header-derived result.
        for name in [
            n
            for n, e in found.items()
            if e["category"] == "runtime" and powered.lower().startswith(n.lower())
        ]:
            found.pop(name)
        found.setdefault(
            powered,
            {"name": powered, "category": "runtime", "evidence": f"x-powered-by: {powered}"},
        )

    # Optionally load a user-supplied WebAppAnalyzer-format fingerprint database from
    # ``SEOHEAD_TECH_DB``. Built-in signatures take precedence; the external database
    # adds only unknown technologies. GPL-licensed database material is not distributed.
    external_report: dict[str, Any] = {"loaded": False}
    from seohead.recon import tech_db

    configured_path = tech_db.get_external_db_path()
    try:
        ext = tech_db.detect_external(html, headers, cookies, scripts, page_url, configured_path)
        if ext.get("db_loaded"):
            external_report = {
                "loaded": True,
                "path": ext.get("db_path"),
                "technologies_in_db": ext.get("technologies_count"),
            }
            for tech in ext["technologies"]:
                if tech["name"] not in found:
                    found[tech["name"]] = tech
    except Exception as exc:
        # A malformed optional database must not break built-in fingerprint
        # detection, but silently reporting it the same as "not configured"
        # (loaded: false with no path) hides a real failure of the operator's
        # own supplied data as the documented normal state. Distinguish the
        # two: no configured path stays the ordinary "not configured" result;
        # a configured path that failed to load or match reports its own
        # failure state, the offending path, and a sanitized error summary
        # (type and message only, never a traceback).
        if configured_path:
            external_report = {
                "loaded": False,
                "state": "external_db_failed",
                "path": configured_path,
                "error": f"{type(exc).__name__}: {exc}",
            }

    # Capture the id, not just the tag name: a name-only match cannot tell two
    # deployments of the same tag apart, which is what a site-wide conflict check
    # (see tag_coverage) needs.
    for name, pattern in IDENTIFIER_PATTERNS.items():
        if name not in found:
            continue
        ids = sorted(set(pattern.findall(html)))
        if ids:
            found[name]["identifiers"] = ids

    by_category: dict[str, list[dict[str, Any]]] = {}
    for entry in found.values():
        by_category.setdefault(entry["category"], []).append(entry)
    for items in by_category.values():
        items.sort(key=lambda e: e["name"].lower())

    third_party = sorted({_host(src) for src in scripts if _host(src)} - {_host(page_url)})
    return {
        "ok": True,
        "url": url or page_url,
        "final_url": page_url,
        "status_code": status_code,
        "generator": generator.group(1).strip() if generator else None,
        "technologies": sorted(found.values(), key=lambda e: (e["category"], e["name"].lower())),
        "by_category": by_category,
        "scripts_total": len(scripts),
        "third_party_hosts": third_party,
        "external_db": external_report,
        # Every row a coverage report builds from this must carry how it was
        # measured: a tag invisible in static markup may still fire for a real
        # visitor, and reporting it as "missing" without this stamp is what
        # costs an audit its credibility.
        "measurement": {
            "representation": "rendered_dom" if rendered else "static_markup",
            "script_executed": rendered,
        },
        "findings": _findings(by_category, third_party),
    }


def _host(src: str) -> str:
    from urllib.parse import urlsplit

    try:
        return urlsplit(src if "//" in src else f"//{src}").netloc.lower()
    except ValueError:
        return ""


def _findings(by_category: dict[str, list[dict[str, Any]]], third_party: list[str]) -> list[str]:
    out: list[str] = []
    if not by_category.get("cms") and not by_category.get("framework"):
        out.append(
            "No CMS or framework was detected; the site may be custom-built or "
            "rendered entirely by JavaScript."
        )
    if not by_category.get("analytics"):
        out.append("No analytics integration was detected on the page.")
    pixels = len(by_category.get("pixel", []))
    if pixels >= 3:
        out.append(
            f"{pixels} advertising pixels are loaded on the page, adding transfer "
            "weight and network requests."
        )
    if len(third_party) >= 10:
        out.append(
            f"Scripts are loaded from {len(third_party)} third-party domains, "
            "which can increase performance and availability risk."
        )
    # A CMS combined with a page-rendering framework suggests a headless architecture.
    # Libraries such as jQuery do not count because they routinely coexist with a
    # traditional CMS.
    ssr = [e["name"] for e in by_category.get("framework", []) if e["name"] in SSR_FRAMEWORKS]
    if by_category.get("cms") and ssr:
        names = [e["name"] for e in by_category["cms"]] + ssr
        out.append(
            f"Detected both {', '.join(names)}, suggesting a headless architecture; "
            "verify rendering and crawlability."
        )
    return out


# Tags an audit is most often asked to prove are, or are not, on every page.
DEFAULT_COVERAGE_TAGS: tuple[str, ...] = (
    "Google Analytics 4",
    "Google Tag Manager",
    "Яндекс.Метрика",
)

# A tag manager is expected to inject these tags client-side, so their absence
# from static markup when the manager is present is not evidence of a gap.
INJECTED_BY: dict[str, tuple[str, ...]] = {
    "Google Analytics 4": ("Google Tag Manager",),
}


def _url_template(url: str) -> str:
    """Collapse a URL path to a template by masking id-shaped segments.

    A dependency-free stand-in for real template detection: a path segment that
    is all digits, or 8+ characters long, is treated as a slug or id, so
    ``/product/42`` and ``/product/leather-boots`` fold into ``/product/*``.
    """
    from urllib.parse import urlsplit

    path = urlsplit(url).path.strip("/")
    if not path:
        return "/"
    parts = ["*" if seg.isdigit() or len(seg) >= 8 else seg for seg in path.split("/")]
    return "/" + "/".join(parts)


def tag_coverage(
    pages: list[dict[str, Any]], *, tags: tuple[str, ...] = DEFAULT_COVERAGE_TAGS
) -> dict[str, Any]:
    """Aggregate per-page :func:`analyze_tech`/:func:`detect_tech` results site-wide.

    Each item in ``pages`` is one of those results, optionally carrying a
    ``template`` key; when absent, one is derived from the URL path. A page whose
    fetch failed (``ok`` false) is excluded from the denominator entirely, the
    same absence rule custom search relies on: a page nobody saw must not be
    counted as missing the tag.
    """
    fetched = [p for p in pages if p.get("ok")]
    stamps = sorted(
        {(p.get("measurement") or {}).get("representation", "static_markup") for p in fetched}
    )

    by_template: dict[str, list[dict[str, Any]]] = {}
    for page in fetched:
        key = page.get("template") or _url_template(page.get("url") or page.get("final_url") or "")
        by_template.setdefault(key, []).append(page)

    def has_tech(page: dict[str, Any], name: str) -> dict[str, Any] | None:
        return next((t for t in page.get("technologies", []) if t.get("name") == name), None)

    rows = []
    for tag in tags:
        identifiers: set[str] = set()
        by_template_row: dict[str, dict[str, Any]] = {}
        pages_with_tag = 0
        for template, group in by_template.items():
            present = 0
            likely_injected = 0
            for page in group:
                entry = has_tech(page, tag)
                if entry:
                    present += 1
                    identifiers.update(entry.get("identifiers", []))
                elif any(has_tech(page, manager) for manager in INJECTED_BY.get(tag, ())):
                    likely_injected += 1
            by_template_row[template] = {
                "pages": len(group),
                "with_tag": present,
                "fraction": round(present / len(group), 4) if group else 0.0,
                "likely_injected_by_manager": likely_injected,
            }
            pages_with_tag += present
        rows.append(
            {
                "tag": tag,
                "pages_with_tag": pages_with_tag,
                "fraction": round(pages_with_tag / len(fetched), 4) if fetched else 0.0,
                "identifiers": sorted(identifiers),
                "conflicting_identifiers": len(identifiers) > 1,
                "by_template": by_template_row,
            }
        )

    return {
        "ok": True,
        "pages_considered": len(fetched),
        "pages_excluded_fetch_failed": len(pages) - len(fetched),
        # A single value when every page was measured the same way; a list when a
        # partial render was mixed into an otherwise static crawl, so the caller
        # cannot mistake a mixed run for a uniformly reliable one.
        "measurement_stamp": stamps[0] if len(stamps) == 1 else stamps,
        "tags": rows,
    }
