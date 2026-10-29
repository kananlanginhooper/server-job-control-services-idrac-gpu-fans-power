#!/bin/bash
# manages: transcribe_gpu\.py
# Owner: 5b- PDF, screenshot OCR categorization. Pauses/resumes the Whisper GPU transcription job on a run-profile change.
# Idempotent: down/down and up/up are harmless. Returns immediately.
set -f
for pid in $(pgrep -f "^python3 transcribe_gpu\.py$" 2>/dev/null); do
  case "$1" in
    down) kill -STOP "$pid" 2>/dev/null ;;
    up)   kill -CONT "$pid" 2>/dev/null ;;
  esac
done
exit 0
