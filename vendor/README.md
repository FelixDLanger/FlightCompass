# Vendored map libraries

Copied from the npm registry (npm verifies each package's integrity) by
`.github/workflows/vendor-libs.yml` on 2026-10-27, and compared byte for byte with the CDN
copies the page loaded before, so the code that runs is unchanged. Licenses are kept next to the files.

| File | npm package | CDN check |
|---|---|---|
| d3-7.8.5.min.js | d3@7.8.5, dist/d3.min.js | identical to https://cdnjs.cloudflare.com/ajax/libs/d3/7.8.5/d3.min.js |
| topojson-3.0.2.min.js | topojson@3.0.2, dist/topojson.min.js | identical to https://cdnjs.cloudflare.com/ajax/libs/topojson/3.0.2/topojson.min.js |
| world-atlas-2.0.2-countries-110m.json | world-atlas@2.0.2, countries-110m.json | identical to https://cdn.jsdelivr.net/npm/world-atlas@2/countries-110m.json |

SHA-256:

```
d6b03aefc9f6c44c7bc78713679c78c295028fa914319119e5cc4b4954855b1c  vendor/d3-7.8.5.min.js
b47a003c6a0d761211dbc60797d0d62f37917ddc228241fb38205732b1d78683  vendor/topojson-3.0.2.min.js
2516c915867c7baf18ddec727aec46c315541a07cfb3d79a6559b05d5e94eee8  vendor/world-atlas-2.0.2-countries-110m.json
```
