(function () {
    "use strict";

    var LOG_PREFIX = "[BlogEditor]";

    function fieldLength(field) {
        if (field.type === "checkbox" || field.type === "radio") {
            return field.checked ? 1 : 0;
        }
        return String(field.value || "").length;
    }

    function isEmptyRequiredField(field, form) {
        if (!field.required || field.disabled) {
            return false;
        }
        if (field.type === "checkbox") {
            return !field.checked;
        }
        if (field.type === "radio") {
            return !form.querySelector(
                'input[type="radio"][name="' + CSS.escape(field.name) + '"]:checked'
            );
        }
        return !String(field.value || "").trim();
    }

    function inspectBody(form) {
        var countField = form.elements.namedItem("body-count");
        var count = countField ? Number.parseInt(countField.value, 10) || 0 : 0;
        var blocks = [];
        var emptyRequired = [];

        for (var index = 0; index < count; index += 1) {
            var prefix = "body-" + index + "-";
            var typeField = form.elements.namedItem(prefix + "type");
            var deletedField = form.elements.namedItem(prefix + "deleted");
            var deleted = Boolean(deletedField && deletedField.value);
            var fields = Array.from(form.elements).filter(function (field) {
                return field.name && field.name.indexOf(prefix) === 0;
            });

            blocks.push({
                index: index,
                type: typeField ? typeField.value : "<missing>",
                deleted: deleted,
                fields: fields
                    .filter(function (field) {
                        return !/(?:-type|-order|-deleted|-id)$/.test(field.name);
                    })
                    .map(function (field) {
                        return {
                            name: field.name.slice(prefix.length),
                            characters: fieldLength(field),
                            required: field.required,
                        };
                    }),
            });

            if (!deleted) {
                fields.forEach(function (field) {
                    if (isEmptyRequiredField(field, form)) {
                        emptyRequired.push(field.name);
                    }
                });
            }
        }

        return {
            bodyCount: count,
            activeBlocks: blocks.filter(function (block) {
                return !block.deleted;
            }).length,
            emptyRequired: Array.from(new Set(emptyRequired)),
            blocks: blocks,
        };
    }

    function inspect(form, eventName) {
        var summary = inspectBody(form);
        var logger = summary.emptyRequired.length ? console.warn : console.info;
        logger.call(console, LOG_PREFIX, eventName, summary);
        return summary;
    }

    function bindForm(form) {
        if (!form || form.dataset.blogEditorDiagnostics === "true") {
            return;
        }
        form.dataset.blogEditorDiagnostics = "true";
        var formDataLogPending = false;

        form.addEventListener("submit", function (event) {
            inspect(form, "form:submit");
            formDataLogPending = true;
            console.info(LOG_PREFIX, "form:submitter", {
                name: event.submitter ? event.submitter.name : null,
                value: event.submitter ? event.submitter.value : null,
            });
        });

        form.addEventListener("formdata", function () {
            if (formDataLogPending) {
                formDataLogPending = false;
                inspect(form, "form:formdata");
            }
        });
    }

    function init() {
        var bodyRoot = document.getElementById("body-root");
        var form = bodyRoot ? bodyRoot.closest("form") : null;
        bindForm(form);

        var summary = form ? inspectBody(form) : null;
        console.info(LOG_PREFIX, "ready", summary || { bodyFieldAvailable: false });

        window.BlogEditorDebug = {
            inspect: function () {
                return form ? inspect(form, "manual:inspect") : null;
            },
        };
    }

    if (document.readyState === "loading") {
        document.addEventListener("DOMContentLoaded", init, { once: true });
    } else {
        init();
    }
})();
