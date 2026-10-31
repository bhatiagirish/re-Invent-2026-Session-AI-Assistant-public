/* Deployment configuration. The API base URL is injected here at deploy time
 * (e.g. by a build/deploy step that rewrites this file) so it is not hard-coded
 * throughout the JavaScript. Contains NO secrets. */
window.APP_CONFIG = {
  // Base URL of the API Gateway stage, e.g. "https://abc123.execute-api.us-east-1.amazonaws.com".
  // Leave empty to use same-origin (when served behind CloudFront with an /api route).
  API_BASE_URL: "",
  EVENT_NAME: "AWS re:Invent 2026",
};
