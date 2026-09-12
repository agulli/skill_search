"""Hand-labelled evaluation set, built by reading the evidence in each case."""
# Genuine attacks or deliberate attack fixtures. Verified by reading the body.
ATTACKS = {
 "prompt-injection@8ddieHu0314/Skill-Lab",
 "malicious-skill@AgentPostmortem/Skill-audit",
 "challenge-08@Berserk-hub150/skillhawk",
 "unsafe-skill@Berserk-hub150/skillhawk",
 "helper@V3r7ig0/skillvet",
 "skillvet@V3r7ig0/skillvet",
 "shell-super-agent@agent-contracts/skill-preflight",
 "MessySkill@alinotfoundbtw/sounding",
 "weather-assistant@cisco-ai-defense/skill-scanner",
 "negative@domehahn/skil",
 "positive@domehahn/skil",
 "unicode-deception@domehahn/skil",
 "prompt-injection-tester@fevziegeyurtsevenler/llm-security-skills",
 "hidden-unicode-instruction@handcraftedbygod/SkillTrace",
 "friendly-writer@munzzyy/skillxray",
 "hidden-unicode@stella/skillguard",
 "hidden-unicode@studiomeyer-io/skilldoctor",
 "injection-body@studiomeyer-io/skilldoctor",
 "audit-injection@xyiqq/skilldoctor",
}
# Legitimate skills that a naive detector blocks. Each was read and the reason
# recorded, because the reason is what generalises.
BENIGN = {
 # Defensive skills that quote the attack in order to describe it.
 "prompt-injection-defense@BagelHole/DevOps-Security-Agent-Skills",
 "defending-llms-with-guardrails@Youngmaidainon/Agent-Level-Up",
 "testing-agents-for-indirect-prompt-injection@UnboundCompute/security-agent-skills",
 "revenantworks-foundation-skillsmith@revenantworks/claude-skills",
 # Security scanners: they contain the signatures they scan for.
 "local-security-check@addxai/enterprise-harness-engineering",
 "repo-forensics@alexgreensh/repo-forensics",
 "agent-skill-auditor@fevziegeyurtsevenler/llm-security-skills",
 # A prohibition, not an instruction: "must not override safety rules".
 "goal-mode@shenwell/ai-agent-skills",
 # A quoted example of a copywriting technique.
 "alterlab-pra-copywriter@AlterLab-IEU/AlterLab-FC-Skills",
 # A/B-test methodology: "instruct each arm to disregard prior findings".
 "skill-authoring@F-e-u-e-r/opus-pack",
 # Invisible characters that are the subject matter, or a paste artifact.
 "i18n-rtl-l10n@zakariaf/Flutter-Skills",
 "telnyx-webrtc-client-js@team-telnyx/ai",
 "cm-codeintell@tody-agent/codymaster",
 # Legitimate red-team tooling, honestly described.
 "offensive-initial-access@SnailSploit/Claude-Red",
}
