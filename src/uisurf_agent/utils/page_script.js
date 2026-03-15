window.WebSurfer = {
    getInteractiveRects: function () {
        const rects = {};
        const interactives = document.querySelectorAll('button, a, input, select, textarea, [role="button"], [role="link"]');
        interactives.forEach((el, index) => {
            const id = `el-${index}`;
            el.setAttribute('__elementId', id);
            const rect = el.getBoundingClientRect();
            rects[id] = {
                x: rect.x,
                y: rect.y,
                width: rect.width,
                height: rect.height,
                tag: el.tagName.toLowerCase(),
                text: el.innerText || el.value || el.placeholder || ""
            };
        });
        return rects;
    },
    getVisualViewport: function () {
        return {
            x: window.scrollX,
            y: window.scrollY,
            width: window.innerWidth,
            height: window.innerHeight,
            pageTop: window.scrollY,
            scrollHeight: document.documentElement.scrollHeight
        };
    },
    getFocusedElementId: function () {
        const el = document.activeElement;
        return el ? el.getAttribute('__elementId') || "" : "";
    },
    getPageMetadata: function () {
        return {
            title: document.title,
            url: window.location.href,
            description: document.querySelector('meta[name="description"]')?.content || ""
        };
    }
};
