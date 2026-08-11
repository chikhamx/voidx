#!/bin/sh
set -eu
cd /Users/chikham/workspace/knowledgebase/apps/web
npm test -- --run
npm run build
