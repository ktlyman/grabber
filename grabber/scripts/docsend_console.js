/**
 * DocSend Console Script — paste into the browser DevTools console while
 * viewing a DocSend document.  It replaces the page with all extracted page
 * images so you can Ctrl+P / Cmd+P to "Print → Save as PDF".
 *
 * Before pasting: type  allow pasting  in the console and press Enter to
 * unlock paste protection.
 */
(async function () {
  // --- Detect page count from toolbar ---
  let totalPages = 0;
  const indicator = document.querySelector(".toolbar-page-indicator");

  if (indicator) {
    const parts = indicator.innerText.split("/");
    if (parts.length > 1) {
      totalPages = parseInt(parts[1].trim(), 10);
    }
  }

  if (!totalPages || isNaN(totalPages)) {
    totalPages = parseInt(
      prompt("Could not auto-detect page count. Enter total pages:", "10"),
      10
    );
  }

  console.log(`Detected ${totalPages} pages.`);

  // --- Build base URL ---
  let baseUrl =
    window.location.href.split("?")[0].replace(/\/$/, "") + "/page_data/";

  // --- Replace page with clean print-friendly layout ---
  document.body.innerHTML = "";
  document.head.innerHTML = `
    <title>DocSend Export</title>
    <style>
      body { background: #f0f0f0; font-family: sans-serif; margin: 0; padding: 20px; text-align: center; }
      img { max-width: 100%; height: auto; margin-bottom: 20px; box-shadow: 0 4px 15px rgba(0,0,0,0.3); display: block; margin-left: auto; margin-right: auto; }
      #progress-bar { position: fixed; top: 0; left: 0; height: 5px; background: #2196F3; width: 0%; transition: width 0.2s; z-index: 9999; }
      #status { position: fixed; top: 10px; right: 10px; background: rgba(0,0,0,0.8); color: white; padding: 10px 20px; border-radius: 5px; z-index: 9999; }
      .page-break { page-break-after: always; }
      @media print {
        body { background: white; padding: 0; }
        img { box-shadow: none; margin-bottom: 0; max-height: 100vh; }
        #progress-bar, #status { display: none; }
      }
    </style>`;

  const progressEl = document.createElement("div");
  progressEl.id = "progress-bar";
  document.body.appendChild(progressEl);

  const statusEl = document.createElement("div");
  statusEl.id = "status";
  statusEl.innerText = `Initializing extraction for ${totalPages} pages...`;
  document.body.appendChild(statusEl);

  const container = document.createElement("div");
  document.body.appendChild(container);

  // --- Fetch each page image ---
  for (let i = 1; i <= totalPages; i++) {
    try {
      statusEl.innerText = `Fetching page ${i} of ${totalPages}...`;
      progressEl.style.width = `${(i / totalPages) * 100}%`;

      const response = await fetch(baseUrl + i);
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      const data = await response.json();

      if (data.imageUrl) {
        const img = document.createElement("img");
        img.src = data.imageUrl;
        const pageBreak = document.createElement("div");
        pageBreak.className = "page-break";
        container.appendChild(img);
        container.appendChild(pageBreak);
      }

      await new Promise((r) => setTimeout(r, 50));
    } catch (err) {
      console.error(`Failed to load page ${i}:`, err);
    }
  }

  statusEl.innerText = "Done! Press Ctrl+P / Cmd+P to save as PDF.";
  statusEl.style.backgroundColor = "#4CAF50";

  // Also save a JSON file with the image URLs for use with:
  //   grabber URL --url-file grabber_urls.json
  const allUrls = Array.from(container.querySelectorAll("img")).map(
    (img) => img.src
  );
  if (allUrls.length) {
    const blob = new Blob([JSON.stringify(allUrls)], {
      type: "application/json",
    });
    const a = document.createElement("a");
    a.href = URL.createObjectURL(blob);
    a.download = "grabber_urls.json";
    document.body.appendChild(a);
    a.click();
    a.remove();
    console.log(`Saved ${allUrls.length} image URLs to grabber_urls.json`);
  }

  alert(
    "Extraction complete.\n\n" +
      "Option 1: Ctrl+P / Cmd+P to print as PDF.\n" +
      "Option 2: Use the downloaded grabber_urls.json with:\n" +
      "  grabber URL --url-file grabber_urls.json -o output.pdf"
  );
})();
