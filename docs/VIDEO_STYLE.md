# Video style guide

How every Evo TikTok video looks and sounds, filmed or AI-made. The script rules
are in `docs/CONTENT_RULES.md`; this file covers the finished video. Agreed with
Layton on 13 October 2026 from the shoe pilot (v4).

## Status of AI-made video

AI footage and AI voiceover are a **pilot** that Layton approved for testing. The
automated jobs still follow guardrail 4 in `CLAUDE.md` (real footage and real
voices) until James and Joems agree to change it. Every AI video:

- is approved by Layton and posted by a person, never automatically;
- is posted with TikTok's "AI-generated" label switched on;
- never shows an AI-made version of a real person (staff, members, winners)
  unless that person has agreed in writing.

## What each video is for

Each video is a window into the Clubhouse: real products, what Clubhouse members
save on them, and one reason to join. It sells; it doesn't narrate.

Script order (value comparisons and Size Roulette):

1. **Hook:** a golfer's problem or want, spoken in the first 2 seconds ("Wet feet by the third hole?").
2. **Benefits:** one or two, taken only from the product page.
3. **Saving:** "Clubhouse members save up to X%", checked against today's member prices for the products shown.
4. **Join:** "Join the Clubhouse, link in bio."

No £ prices anywhere, except "£36 a year" on the end card.

## Voice

- ElevenLabs **Charlotte** (`elevenlabs/tts/eleven-v4` on fal.ai), stability 0.4, British English.
- Warm, quick and plain-spoken: a golf shop team member, not an advert.
- Numbers as people say them ("twenty-five percent", "the third hole").
- Listen for brand terms before approving (UA HOVR should sound like "hover"). Spell a word phonetically in the script if it's misread.
- Under 22 seconds of speech; aim for 15 to 20.

## Footage

- **Setting:** the Evo studio, as in Alex's reels: bright green turf hitting mat, dark grey walls,
  the golf simulator screen and hexagonal LED ceiling lights. Never a generic golf course.
- **AI footage:** MiniMax H3 Max reference-to-video on fal.ai, 9:16, 768P, 5-second clips. References:
  the product's Shopify photos (side view and pair) plus two studio frames. Prompt with
  "the shoes must look exactly like the reference photos", "no text, no people, no hands, no extra products".
- **Shots:** a hero shot of everything featured, one close-up orbit per product, and a wide studio
  shot to close. Slow camera moves only.
- **Before approving,** compare every product against the real thing: shape, colour, logos, sole.
  Cut any shot where the product is wrong or something odd appears (floating balls, extra laces).

## On-screen text (style D, "minimal premium")

Canvas 1080 × 1920. No boxes or banners; text sits on a soft dark fade.

| Element | Spec |
|---|---|
| Fade | Black, from y = 1000 to the bottom, rising to about 85% opacity |
| Label | Inter Bold 34 px, letter-spaced 5 px, uppercase, yellow `#F5DB00`, at x 70, y 1300 |
| Headline | Inter ExtraBold 82 px, white, at x 70, y 1348; one line, at most about 800 px wide |
| Logo | Evolution Golf logo, 250 px wide, top left at x 70, y 200 |
| Motion | Each label and headline fades in and slides up 28 px over 0.3 s, easing out |

Rules:

- **Never caption the voiceover.** On-screen text adds what the voice doesn't say: a size range in stock,
  a spec from the product page, the member saving.
- One label and one headline at a time, changing with the shot.
- No brand name next to a discount.
- **TikTok safe area:** keep text between x 70 and 900 and above y 1520. TikTok's caption and username
  cover the bottom, and its like and share buttons cover the right-hand side.

## End card (Clubhouse sign-up page style)

3 seconds on Clubhouse green `#016747`:

- the white Clubhouse seal, 520 px, centred, fading in;
- "MEMBERS ONLY" in the label style, centred;
- "Join the Clubhouse." in Inter ExtraBold 84 px, white;
- "£36 a year · link in bio" in Inter Medium 44 px.

## Colours and fonts

| Use | Value |
|---|---|
| Clubhouse green | `#016747` (website `#006747`) |
| Dark green | `#1B3D2B` |
| Bright green | `#3DA35D` |
| Logo yellow | `#F5DB00` |
| Typeface | Inter (as on the Clubhouse sign-up page); Montserrat is the website's display face |

## Sound

- Voice levelled to -14 LUFS.
- Music is added from TikTok's library when posting (licensed for TikTok); nothing baked in.

## Approval checklist

- [ ] Every product looks like the real one; no odd objects in shot.
- [ ] The saving matches the saving check in the pack, and there's no £ figure except the end card's £36.
- [ ] Every benefit is on the product page.
- [ ] No on-screen line repeats the voice; all text sits inside the safe area.
- [ ] Brand terms sound right.
- [ ] Posted with TikTok's "AI-generated" label on, and music from TikTok's library.
