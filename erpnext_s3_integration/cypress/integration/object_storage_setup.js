context("Object Storage Setup Assistant", () => {
	before(() => {
		cy.login();
		cy.visit("/app/object-storage-settings");
	});

	it("shows four clear feature states and International Alibaba Cloud console actions", () => {
		cy.findByText("Setup Assistant").click();
		cy.findByText("Attachment Storage");
		cy.findByText("Database Backups");
		cy.findByText("Attachment Archive");
		cy.findByText("Archive Restore");
		cy.findByRole("button", { name: "More" }).click();
		cy.findByText("Open OSS Console");
		cy.findByText("Open RAM Console");
	});

	it("keeps classification discoverable without a view mode", () => {
		cy.findByText("Attachments").click();
		cy.findByText("Attachment Classification");
		cy.findByText("Long-term Attachment Storage");
		cy.findByText("Unmatched attachments");
		cy.findByRole("button", { name: "Guided" }).should("not.exist");
		cy.findByRole("button", { name: "Advanced" }).should("not.exist");
		cy.get("[data-fieldname=retention_rules]").should("not.be.visible");
		cy.findByRole("button", { name: "Edit Classification Rules" }).click();
		cy.get("[data-fieldname=retention_rules]").should("be.visible");
	});

	it("uses clear backup names without a duplicate bucket field", () => {
		cy.findByText("Backups").click();
		cy.findByText("Database Backup");
		cy.findByText("Local Public Files Folder Backup");
		cy.findByText("Local Private Files Folder Backup");
		cy.get("[data-fieldname=backup_bucket]").should("not.exist");
	});

	it("recommends the Jakarta public endpoint for a development profile", () => {
		cy.visit("/app/object-storage-profile/new-object-storage-profile");
		cy.get("[data-fieldname=environment] select").select("Development");
		cy.get("[data-fieldname=provider]").should("contain", "Alibaba Cloud OSS");
		cy.get("[data-fieldname=region]").should("contain", "ap-southeast-5");
		cy.get("[data-fieldname=use_internal_endpoint] input").should("not.be.checked");
		cy.get("[data-fieldname=endpoint_url] input").should(
			"have.value",
			"https://oss-ap-southeast-5.aliyuncs.com"
		);
	});
});
