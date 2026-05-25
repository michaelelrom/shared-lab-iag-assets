# Trivial smoke-test plan — no providers, no external state.
# Verifies OpenTofu can parse and evaluate a plan invoked from IAG5.

variable "name" {
  description = "Greeting subject"
  type        = string
  default     = "iag5"
}

output "greeting" {
  value = "terraform test ok for ${var.name}"
}

output "timestamp" {
  value = timestamp()
}
