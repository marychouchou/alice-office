// maps — place search and details via the Google Places API (New).
// API reference: https://developers.google.com/maps/documentation/places/web-service/overview
// Requires GOOGLE_MAPS_API_KEY environment variable.
//
// Billing tiers to be aware of (affects which fields we request by default):
//   Essentials   — id, displayName, formattedAddress, location, types
//   Pro          — rating, priceLevel, websiteUri, currentOpeningHours,
//                  nationalPhoneNumber, userRatingCount
//   Enterprise+  — reviews, reviewSummary  ← only requested when includeReviews=true
import { z } from "zod";

const PLACES_BASE = "https://places.googleapis.com/v1/places";

/** Wrap any JSON-serialisable payload as an MCP text result. */
const ok = (payload) => ({ content: [{ type: "text", text: JSON.stringify(payload, null, 2) }] });

/**
 * Map the API's price-level enum string to a 0–4 integer for easier display.
 * Returns null if the value is absent or unrecognised.
 * @param {string|undefined} level
 * @returns {number|null}
 */
const priceLevelInt = (level) => ({
  PRICE_LEVEL_FREE: 0,
  PRICE_LEVEL_INEXPENSIVE: 1,
  PRICE_LEVEL_MODERATE: 2,
  PRICE_LEVEL_EXPENSIVE: 3,
  PRICE_LEVEL_VERY_EXPENSIVE: 4,
}[level] ?? null);

/**
 * Parse a "lat,lng" string into { latitude, longitude } numbers.
 * Returns null on invalid input.
 * @param {string|undefined} s
 * @returns {{ latitude: number, longitude: number }|null}
 */
function parseLatLng(s) {
  if (!s) return null;
  const [lat, lng] = s.split(",").map(Number);
  if (!isFinite(lat) || !isFinite(lng)) return null;
  return { latitude: lat, longitude: lng };
}

// ---------------------------------------------------------------------------
// Handlers
// ---------------------------------------------------------------------------

/**
 * Register all maps_* tools on the MCP server.
 * The Maps API key is read once from env at startup and closed over.
 * @param {import("@modelcontextprotocol/sdk/server/mcp.js").McpServer} server
 * @param {string} apiKey  value of GOOGLE_MAPS_API_KEY
 */
export function registerMapsTools(server, apiKey) {
  server.tool(
    "maps_search",
    "Search for places (restaurants, cafes, etc.) using Google Places Text Search (New API). " +
    "Returns up to pageSize results with name, address, rating, price level, and open-now status. " +
    "To read reviews for a specific result, call maps_details with includeReviews=true.",
    {
      query: z.string().describe("Search query, e.g. 'ramen near Taipei 101'"),
      location: z.string().optional().describe("Bias point as 'lat,lng', e.g. '25.033,121.565'"),
      radius: z.number().optional().describe("Bias radius in metres (only used together with location)"),
      type: z.string().optional().describe("Included place type, e.g. restaurant, cafe, bar"),
      openNow: z.boolean().optional().describe("If true, return only places currently open"),
      pageSize: z.number().int().min(1).max(20).optional()
        .describe("Number of results to return (1–20, default 5). Use a larger value only when the user asks for more options."),
      language: z.string().optional().describe("BCP-47 language code for results; defaults to zh-TW"),
    },
    (args) => textSearch(apiKey, args),
  );

  server.tool(
    "maps_details",
    "Fetch full details for a place by its Place ID (obtained from maps_search). " +
    "Always returns address, phone, hours, website, and rating. " +
    "Set includeReviews=true ONLY when the user explicitly asks about reviews, comments, or what other people say — " +
    "reviews trigger a significantly higher billing tier (Enterprise+Atmosphere).",
    {
      placeId: z.string().describe("Google Place ID from a maps_search result"),
      includeReviews: z.boolean().optional()
        .describe(
          "Include user reviews and AI review summary. " +
          "DEFAULT FALSE. Set true only when the user explicitly asks about reviews or opinions.",
        ),
      language: z.string().optional().describe("BCP-47 language code; defaults to zh-TW"),
    },
    (args) => placeDetails(apiKey, args),
  );
}

/**
 * Search for places using the Places API (New) Text Search endpoint.
 * Uses Essentials + Pro field tiers by default; reviews are NOT requested here.
 * @param {string} apiKey
 * @param {{ query: string, location?: string, radius?: number, type?: string,
 *           openNow?: boolean, pageSize?: number, language?: string }} args
 */
async function textSearch(apiKey, { query, location, radius, type, openNow, pageSize, language }) {
  if (!apiKey) return ok({ error: "GOOGLE_MAPS_API_KEY is not configured." });
  if (!query) return ok({ error: "query is required." });

  const fieldMask = [
    "places.id",
    "places.displayName",
    "places.formattedAddress",
    "places.location",
    "places.types",
    "places.rating",
    "places.userRatingCount",
    "places.priceLevel",
    "places.currentOpeningHours.openNow",
  ].join(",");

  const body = {
    textQuery: query,
    pageSize: pageSize ?? 5,
    languageCode: language || "zh-TW",
  };

  if (type) body.includedType = type;
  if (openNow) body.openNow = true;

  const coords = parseLatLng(location);
  if (coords) {
    body.locationBias = {
      circle: {
        center: coords,
        radius: radius ?? 5000,
      },
    };
  }

  const res = await fetch(`${PLACES_BASE}:searchText`, {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      "X-Goog-Api-Key": apiKey,
      "X-Goog-FieldMask": fieldMask,
    },
    body: JSON.stringify(body),
  });

  if (!res.ok) {
    const err = await res.json().catch(() => ({}));
    return ok({ error: `Places API error ${res.status}`, detail: err.error?.message ?? "" });
  }

  const data = await res.json();
  const places = (data.places ?? []).map((p) => ({
    placeId: p.id,
    name: p.displayName?.text ?? null,
    address: p.formattedAddress ?? null,
    rating: p.rating ?? null,
    totalRatings: p.userRatingCount ?? null,
    priceLevel: priceLevelInt(p.priceLevel),
    openNow: p.currentOpeningHours?.openNow ?? null,
    location: p.location ?? null,
    types: (p.types ?? []).slice(0, 5),
  }));

  return ok({ query, count: places.length, places });
}

/**
 * Fetch detailed information for a single place by Place ID.
 * Requests Essentials + Pro tier fields by default.
 * Adding reviews (Enterprise+Atmosphere tier) requires includeReviews=true.
 * @param {string} apiKey
 * @param {{ placeId: string, includeReviews?: boolean, language?: string }} args
 */
async function placeDetails(apiKey, { placeId, includeReviews, language }) {
  if (!apiKey) return ok({ error: "GOOGLE_MAPS_API_KEY is not configured." });
  if (!placeId) return ok({ error: "placeId is required." });

  const fields = [
    "id",
    "displayName",
    "formattedAddress",
    "nationalPhoneNumber",
    "rating",
    "userRatingCount",
    "priceLevel",
    "currentOpeningHours",
    "websiteUri",
    "googleMapsUri",
  ];
  if (includeReviews) {
    // Enterprise+Atmosphere tier — only added when explicitly requested.
    fields.push("reviews", "reviewSummary");
  }

  const lang = language || "zh-TW";
  const res = await fetch(`${PLACES_BASE}/${placeId}?languageCode=${lang}`, {
    headers: {
      "X-Goog-Api-Key": apiKey,
      "X-Goog-FieldMask": fields.join(","),
    },
  });

  if (!res.ok) {
    const err = await res.json().catch(() => ({}));
    return ok({ error: `Places API error ${res.status}`, detail: err.error?.message ?? "" });
  }

  const p = await res.json();
  const result = {
    placeId: p.id,
    name: p.displayName?.text ?? null,
    address: p.formattedAddress ?? null,
    phone: p.nationalPhoneNumber ?? null,
    rating: p.rating ?? null,
    totalRatings: p.userRatingCount ?? null,
    priceLevel: priceLevelInt(p.priceLevel),
    website: p.websiteUri ?? null,
    googleMapsUrl: p.googleMapsUri ?? null,
    openNow: p.currentOpeningHours?.openNow ?? null,
    weekdayHours: p.currentOpeningHours?.weekdayDescriptions ?? null,
  };

  if (includeReviews) {
    result.reviews = (p.reviews ?? []).slice(0, 5).map((r) => ({
      author: r.authorAttribution?.displayName ?? null,
      rating: r.rating ?? null,
      text: r.text?.text?.slice(0, 200) ?? null,
      relativeTime: r.relativePublishTimeDescription ?? null,
    }));
    result.reviewSummary = p.reviewSummary?.text?.text ?? null;
  }

  return ok(result);
}
