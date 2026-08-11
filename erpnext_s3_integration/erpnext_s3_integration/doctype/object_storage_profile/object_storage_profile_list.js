frappe.listview_settings["Object Storage Profile"] = {
	add_fields: ["enabled", "last_test_status", "provider", "purpose"],
	get_indicator(doc) {
		if (!doc.enabled) return [__("Disabled"), "gray", "enabled,=,0"];
		if (doc.last_test_status === "Passed") return [__("Ready"), "green", "last_test_status,=,Passed"];
		if (doc.last_test_status === "Failed") return [__("Test Failed"), "red", "last_test_status,=,Failed"];
		return [__("Test Required"), "orange", "last_test_status,!=,Passed"];
	},
};
