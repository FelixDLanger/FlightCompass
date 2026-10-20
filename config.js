// FlightCompass site settings. THIS FILE IS PUBLIC: anyone can open it on the live site.
//
// marker: your Travelpayouts PARTNER ID, the number shown in the lower-left corner of the
//         Travelpayouts dashboard. It is printed in every affiliate link anyway, so it is safe to
//         publish. Keep the quotes, e.g.  marker: "1234567"
//         Leave it "" to run without affiliate links; the footer disclosure then stays hidden.
//
// NEVER put the API token here. The token (Travelpayouts -> Profile -> API token, a 32-character
// code) is secret and lives only in GitHub -> Settings -> Secrets and variables -> Actions ->
// TRAVELPAYOUTS_TOKEN, where only the weekly refresh can read it.
//
// Upload this file once, next to index.html. Future index.html updates never touch it.
window.FC_CONFIG = {
  marker: "731824"
};
