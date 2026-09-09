# Test fixtures

`grace_hopper.jpg` -- public-domain US Navy photo of Grace Hopper
(sourced via Wikimedia Commons: https://commons.wikimedia.org/wiki/File:Grace_Hopper.jpg),
vendored here as a real photo containing a face for CV pipeline tests
(face landmark detection, capture-quality assessment, end-to-end
`/analyze`). Previously these tests read this same image out of
`matplotlib`'s bundled sample data inside the local dev `.venv`; that
broke in CI, which installs dependencies without creating a `.venv`
at all, so the hardcoded `.venv/lib/...` path never existed there.
Vendoring the file removes the dependency on any third-party
package's internal data layout or the existence of a project venv.
