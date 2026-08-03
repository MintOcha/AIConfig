# Kinetic subtitle timing format

Pass a JSON array or an object containing a `captions` array. Times are absolute seconds from the start of the input video.

```json
{
  "captions": [
    {
      "start": 5.0,
      "end": 8.04,
      "words": [
        {"text": "One", "start": 5.0, "end": 5.96},
        {"text": "man", "start": 5.96, "end": 6.62},
        {"text": "on", "start": 6.62, "end": 7.04},
        {"text": "an", "start": 7.04, "end": 7.36},
        {"text": "island", "start": 7.36, "end": 8.04}
      ]
    }
  ]
}
```

Keep each word inside its parent caption interval. Use transcription as a first pass, then manually anchor distinctive consonants and phrase releases against the official audio.
