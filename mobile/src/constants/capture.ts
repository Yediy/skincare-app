/**
 * Bounded image size for analysis submission (section 25). Chosen to
 * stay generous enough for the backend's real CV pipeline (face
 * landmark/pose/texture metrics genuinely benefit from detail) while
 * keeping the base64-encoded payload well clear of any reasonable
 * request-body limit -- not tuned to any specific measured backend
 * threshold, since none is documented; if backend CV accuracy or
 * upload latency ever motivates a different bound, this is the one
 * place to change it.
 */

/** Long-edge cap in pixels. A typical modern phone camera capture is
 * 3000-4000px on its long edge -- this resizes down to something a
 * face-analysis pipeline doesn't need more resolution than, without
 * compressing "until the face becomes six pixels and optimism." */
export const MAX_IMAGE_DIMENSION = 1600;

/** JPEG quality, 0-1. 0.8 is a standard "visually lossless for
 * photographic content" choice -- noticeably smaller than 1.0 with no
 * perceptible artifacting at this resolution. */
export const JPEG_QUALITY = 0.8;

/** Documented expectation, not an enforced hard cap: a 1600px-long-edge
 * JPEG at quality 0.8 typically base64-encodes to roughly 200-500KB.
 * If a resized capture's base64 ever exceeds this by a large margin
 * (e.g. an unusually detailed/noisy photo), that's a signal to revisit
 * MAX_IMAGE_DIMENSION/JPEG_QUALITY, not a runtime-enforced rejection --
 * this pass does not add a hard client-side size gate on top of the
 * backend's own request handling.
 */
export const APPROX_MAX_ENCODED_SIZE_BYTES = 700_000;
