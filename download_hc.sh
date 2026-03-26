#!/bin/bash
# Download Bombay HC (court=27_1) and Karnataka HC (court=29_3)
# metadata parquets + tar files from the public S3 bucket.
#
# Usage: bash download_hc.sh
# Estimated size: ~1 GB metadata + ~112 GB tar files

set -e

HC_DIR="/Users/ayush.tripathi/Desktop/legal/hc_data"
BUCKET="s3://indian-high-court-judgments"
COURTS="27_1 29_3"

echo "=== Step 1: Downloading metadata parquets (~1 GB) ==="
for court in $COURTS; do
    echo "  Syncing metadata for court=$court ..."
    aws s3 sync --no-sign-request \
        "$BUCKET/metadata/parquet/" \
        "$HC_DIR/metadata/" \
        --exclude "*" \
        --include "*/court=${court}/*" \
        --only-show-errors
done

echo ""
echo "=== Step 2: Downloading tar files (~112 GB) ==="
for court in $COURTS; do
    echo "  Syncing tars for court=$court ..."
    aws s3 sync --no-sign-request \
        "$BUCKET/data/tar/" \
        "$HC_DIR/tar/" \
        --exclude "*" \
        --include "*/court=${court}/*" \
        --only-show-errors
done

echo ""
echo "Download complete. Files saved to: $HC_DIR"
du -sh "$HC_DIR"
