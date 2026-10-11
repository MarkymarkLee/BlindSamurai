#!/bin/sh
# Builds HeadTracker.app. It has to be an app bundle: macOS only grants headphone-motion
# access to a process whose own Info.plist carries NSMotionUsageDescription.
set -e
cd "$(dirname "$0")"
APP=HeadTracker.app
rm -rf "$APP"
mkdir -p "$APP/Contents/MacOS"
cp Info.plist "$APP/Contents/Info.plist"
swiftc -O -swift-version 5 -o "$APP/Contents/MacOS/HeadTracker" main.swift voice.swift
codesign --force --sign - "$APP"
echo "built $(pwd)/$APP"
