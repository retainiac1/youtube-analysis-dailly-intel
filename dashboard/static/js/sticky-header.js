// Publishes the pinned header's live height to a single CSS custom property
// (--header-h on :root) so the layout never hardcodes it. The filter rail's
// sticky `top` and the scroll root's `scroll-padding-top` both read this var, so
// they follow the header automatically when it wraps, the viewport resizes, the
// theme changes (blur/font), or fonts load late. Isolated from the rest of the
// app: a side-effect import with no exports and no shared state.

const header = document.querySelector(".sticky-header");

if (header) {
  const root = document.documentElement;

  const publishHeight = () => {
    root.style.setProperty("--header-h", `${header.getBoundingClientRect().height}px`);
  };

  // Set it once synchronously so the rail offset and scroll-padding are correct on
  // first load, not only after the first resize.
  publishHeight();

  // ResizeObserver fires an initial callback on observe() and on every later size
  // change, covering wrap / resize / theme / late-font reflows with no re-measure.
  new ResizeObserver(publishHeight).observe(header);
}
