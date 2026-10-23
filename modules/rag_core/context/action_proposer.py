# modules/rag_core/context/action_proposer.py
import re
import logging
from typing import List, Dict, Any, Optional

from modules.rag_core.context.models import (
    SessionWorkingMemory,
    EntityScope,
    ActionType,
    SuggestedAction
)
from modules.rag_core.providers.llm import BaseLLMService

logger = logging.getLogger("action_proposer")

class ActionProposer:
    """
    Proactive Agent Cognition Engine.
    Synthesizes structured 'Next Best Actions' (interactive prompt suggestions and workflow steps)
    leveraging the post-harvest blackboard state (SessionWorkingMemory).
    
    Produces consultative, natural language follow-up questions that are embedded
    directly into the closing section of the assistant's Markdown response, while also
    maintaining structured suggested actions for context grounding and programmatic clients.
    """

    GENERATIVE_PROMPT_INSTRUCTION = (
        "You are an enterprise AI workflow assistant. Given the user's latest query and the system's answer, "
        "propose exactly 2 consultative, natural follow-up questions that a banking or corporate analyst "
        "would logically ask next.\n\n"
        "Format each on a new line starting with: 1. [Label]: [Consultative Question]\n"
        "Rules:\n"
        "- Label must be 2 to 4 words (e.g. 'Check Loan Facilities', 'Review Risk Guidelines').\n"
        "- Consultative Question must be a natural, polite follow-up (e.g. 'Would you like me to check his active loan facilities?', 'Should we review our credit risk policies?').\n"
        "- Output ONLY the 2 numbered lines. No conversational filler or commentary."
    )

    @classmethod
    def format_embedded_recommendations(cls, actions: List[Any]) -> str:
        """
        Formats proactive follow-up recommendations into a natural, consultative
        closing section embedded directly into the assistant's Markdown response.
        """
        if not actions:
            return ""
        lines = ["\n\n---\n**Next steps you might consider:**"]
        for act in actions:
            if isinstance(act, dict):
                text = act.get("conversational_prompt") or act.get("suggested_prompt")
            else:
                text = getattr(act, "conversational_prompt", None) or getattr(act, "suggested_prompt", None)
            if text:
                lines.append(f"- {text}")
        return "\n".join(lines) if len(lines) > 1 else ""

    @classmethod
    def propose_actions(
        cls,
        memory: SessionWorkingMemory,
        query: str,
        answer: str,
        strategy_used: str,
        llm_service: Optional[BaseLLMService] = None
    ) -> List[SuggestedAction]:
        """
        Main entrypoint: analyzes blackboard state and produces 2-3 structured next actions.
        Guarantees zero-leakage scope isolation for SYSTEM_META and AGGREGATE scopes.
        """
        actions: List[SuggestedAction] = []

        try:
            # -----------------------------------------------------------------
            # TIER 1: Deterministic Workflow Traps (0 ms execution)
            # -----------------------------------------------------------------
            actions = cls._evaluate_deterministic_traps(memory, query, strategy_used)

            # -----------------------------------------------------------------
            # TIER 2: Generative Fallback for Unstructured / Conversational Turns
            # -----------------------------------------------------------------
            if len(actions) < 2 and llm_service is not None:
                generative_actions = cls._evaluate_generative_fallback(query, answer, llm_service)
                for ga in generative_actions:
                    if len(actions) >= 3:
                        break
                    actions.append(ga)

        except Exception as e:
            logger.warning(f"[ACTION PROPOSER] Failed to generate actions: {e}")

        # Fallback safeguard: guarantee at least 1 helpful consultative action
        if not actions:
            actions.append(
                SuggestedAction(
                    label="Explore Enterprise Policies",
                    suggested_prompt="What enterprise operational policies and compliance guidelines apply here?",
                    conversational_prompt="Would you like to explore the enterprise operational policies and compliance guidelines that apply here?",
                    action_type=ActionType.PROMPT_SUGGESTION,
                    target_strategy="enterprise",
                    confidence=0.75
                )
            )

        logger.info(
            f"[ACTION PROPOSER] Proposed {len(actions)} actions for session '{memory.session_id}' "
            f"(Scope: {memory.scope}, Strategy: {strategy_used}): {[a.label for a in actions]}"
        )
        return actions

    @classmethod
    def _evaluate_deterministic_traps(
        cls,
        memory: SessionWorkingMemory,
        query: str,
        strategy_used: str
    ) -> List[SuggestedAction]:
        """
        Deterministic state-machine transitions based on blackboard entities and scope.
        0 ms compute overhead; fully deterministic with consultative phrasing.
        """
        actions: List[SuggestedAction] = []
        scope = memory.scope
        entities = memory.active_entities or {}
        q_lower = query.lower()

        # 0. Schema / Metadata / Profile Attributes inquiry detection
        # Prevents multi-row schema sample queries from defaulting to individual or collection filters
        is_schema_query = any(term in q_lower for term in [
            "attributes", "attribute", "columns", "column", "what is stored", 
            "schema", "fields", "field", "profile attributes", "kyc attributes", "data dictionary"
        ])
        if is_schema_query:
            actions.append(
                SuggestedAction(
                    label="Lookup Customer by BVN",
                    suggested_prompt="Who is the customer associated with Bank Verification Number (BVN) 90000001008?",
                    conversational_prompt="Would you like to look up an individual customer profile by BVN or customer number?",
                    action_type=ActionType.WORKFLOW_STEP,
                    target_strategy="sql",
                    confidence=1.0
                )
            )
            actions.append(
                SuggestedAction(
                    label="Inspect Core Banking Schema",
                    suggested_prompt="What tables and account fields are stored in our core banking database?",
                    conversational_prompt="Should we inspect our core banking accounts or lending database schemas?",
                    action_type=ActionType.PROMPT_SUGGESTION,
                    target_strategy="sql",
                    confidence=0.95
                )
            )
            return actions

        # 1. SYSTEM_META Scope: Zero business entity leakage
        if scope == EntityScope.SYSTEM_META.value or strategy_used == "system_meta":
            actions.append(
                SuggestedAction(
                    label="Inspect Customer Profiles",
                    suggested_prompt="What customer profile and KYC attributes are stored in our customer database?",
                    conversational_prompt="Would you like to inspect the customer profile and KYC attributes stored in our customer database?",
                    action_type=ActionType.WORKFLOW_STEP,
                    target_strategy="sql",
                    confidence=1.0
                )
            )
            actions.append(
                SuggestedAction(
                    label="Review Data Governance",
                    suggested_prompt="What data governance and access control policies apply to these data sources?",
                    conversational_prompt="Should we review the data governance and access control policies that apply to these data sources?",
                    action_type=ActionType.PROMPT_SUGGESTION,
                    target_strategy="enterprise",
                    confidence=0.95
                )
            )
            return actions

        # 2. AGGREGATE Scope: Macro-level analysis, no single-entity binds
        if scope == EntityScope.AGGREGATE.value:
            actions.append(
                SuggestedAction(
                    label="Filter by Industry Segment",
                    suggested_prompt="How are these customers distributed across corporate, SME, and retail segments?",
                    conversational_prompt="Would you like to see how our customers are distributed across corporate, SME, and retail segments?",
                    action_type=ActionType.PROMPT_SUGGESTION,
                    target_strategy="sql",
                    confidence=1.0
                )
            )
            actions.append(
                SuggestedAction(
                    label="View Top Corporate Employers",
                    suggested_prompt="Which corporate employers have the largest number of associated customer accounts?",
                    conversational_prompt="Should we examine which corporate employers have the highest account volumes?",
                    action_type=ActionType.PROMPT_SUGGESTION,
                    target_strategy="sql",
                    confidence=0.95
                )
            )
            return actions

        # 3. INDIVIDUAL Scope: Entity progression along the banking / enterprise lifecycle
        has_customer = "customer_id" in entities or "bvn" in entities or "customer_number" in entities
        has_account = "account_id" in entities or "account_number" in entities
        has_loan = "loan_id" in entities or "loan_account_number" in entities

        is_tx_query = any(term in q_lower for term in [
            "transaction", "transactions", "inflow", "outflow", "velocity", "ledger", "past 12 months"
        ])
        is_loan_query = any(term in q_lower for term in [
            "loan", "loans", "lending", "credit facility", "credit facilities", "mortgage"
        ])

        # State 4: Loan facility resolved or loan inquiry made -> Propose policy compliance & audit
        if (has_loan or is_loan_query) and has_customer:
            actions.append(
                SuggestedAction(
                    label="Check Lending Policy Guidelines",
                    suggested_prompt="What are our enterprise lending policies and repayment guidelines for commercial loans?",
                    conversational_prompt="Would you like to review our enterprise lending policies and repayment guidelines for commercial loans?",
                    action_type=ActionType.WORKFLOW_STEP,
                    target_strategy="enterprise",
                    confidence=1.0
                )
            )
            actions.append(
                SuggestedAction(
                    label="Audit Customer Compliance",
                    suggested_prompt="Verify the KYC document status and compliance screening for this customer.",
                    conversational_prompt="Should we audit the customer's KYC verification status and compliance screening records?",
                    action_type=ActionType.WORKFLOW_STEP,
                    target_strategy="sql",
                    confidence=0.95
                )
            )
            return actions

        # State 3: Deposit account resolved or transaction inquiry made
        if (has_account or is_tx_query) and has_customer:
            # If current query was already about transaction activity / inflow history
            if is_tx_query:
                actions.append(
                    SuggestedAction(
                        label="Check Active Loans",
                        suggested_prompt="Does this same customer hold any active loans or credit facilities in our lending system?",
                        conversational_prompt="Should we check whether this customer holds any active loan facilities in our lending system?",
                        action_type=ActionType.WORKFLOW_STEP,
                        target_strategy="sql",
                        confidence=1.0
                    )
                )
                actions.append(
                    SuggestedAction(
                        label="Review Credit Risk Policy",
                        suggested_prompt="What are our enterprise credit risk policies for corporate loan facilities?",
                        conversational_prompt="Would you like to review our enterprise credit risk policies for corporate loan facilities?",
                        action_type=ActionType.PROMPT_SUGGESTION,
                        target_strategy="enterprise",
                        confidence=0.90
                    )
                )
            else:
                # Account balance discovered -> Propose transaction activity or active loans
                actions.append(
                    SuggestedAction(
                        label="Analyze Transaction Velocity",
                        suggested_prompt="Show me his transaction activity. What is his total inflow over the past 12 months, and what was his single largest credit deposit?",
                        conversational_prompt="Would you like me to analyze his transaction activity and 12-month inflow velocity?",
                        action_type=ActionType.WORKFLOW_STEP,
                        target_strategy="sql",
                        confidence=1.0
                    )
                )
                actions.append(
                    SuggestedAction(
                        label="Check Active Loans",
                        suggested_prompt="Does this same customer hold any active loans or credit facilities in our lending system?",
                        conversational_prompt="Should we review his credit facilities or loan exposure in the lending system?",
                        action_type=ActionType.WORKFLOW_STEP,
                        target_strategy="sql",
                        confidence=0.95
                    )
                )
            return actions

        # State 2: Customer identity resolved via BVN / KYC
        if has_customer:
            actions.append(
                SuggestedAction(
                    label="View Deposit Accounts",
                    suggested_prompt="What bank accounts does he have with us, and what is his current total deposit balance?",
                    conversational_prompt="Would you like me to check his active bank accounts and current deposit balances?",
                    action_type=ActionType.WORKFLOW_STEP,
                    target_strategy="sql",
                    confidence=1.0
                )
            )
            actions.append(
                SuggestedAction(
                    label="Check Loan Portfolio",
                    suggested_prompt="Does this customer hold any active loans or credit facilities in our lending system?",
                    conversational_prompt="Should we check if he holds any active credit facilities or loan accounts in our lending system?",
                    action_type=ActionType.WORKFLOW_STEP,
                    target_strategy="sql",
                    confidence=0.95
                )
            )
            return actions

        # 4. COLLECTION Scope: Multi-entity group drill-downs (for multi-customer queries)
        if scope == EntityScope.COLLECTION.value:
            actions.append(
                SuggestedAction(
                    label="List Branch Addresses",
                    suggested_prompt="List the branch addresses and office locations for these customers.",
                    conversational_prompt="Would you like me to list the branch addresses and office locations for these customers?",
                    action_type=ActionType.WORKFLOW_STEP,
                    target_strategy="sql",
                    confidence=1.0
                )
            )
            actions.append(
                SuggestedAction(
                    label="Corporate Industry Analysis",
                    suggested_prompt="Apart from these, which other industries or corporate employers do we have customers from?",
                    conversational_prompt="Should we analyze which other industries or corporate employers we serve?",
                    action_type=ActionType.PROMPT_SUGGESTION,
                    target_strategy="sql",
                    confidence=0.95
                )
            )
            return actions

        return actions

    @classmethod
    def _evaluate_generative_fallback(
        cls,
        query: str,
        answer: str,
        llm_service: BaseLLMService
    ) -> List[SuggestedAction]:
        """
        Lightweight LLM call to synthesize 2 consultative follow-up suggestions
        for unstructured RAG, session documents, or conversational queries.
        """
        actions: List[SuggestedAction] = []
        user_prompt = f"User Question: {query}\nAssistant Answer:\n{answer[:400]}"

        try:
            raw_text = ""
            if hasattr(llm_service, "generate_answer"):
                raw_text = llm_service.generate_answer(
                    system_instruction=cls.GENERATIVE_PROMPT_INSTRUCTION,
                    user_prompt=user_prompt
                )
            elif hasattr(llm_service, "generate_text"):
                raw_text = llm_service.generate_text(
                    prompt=user_prompt,
                    system_instruction=cls.GENERATIVE_PROMPT_INSTRUCTION
                )

            # Parse lines like "1. [Label]: [Consultative Question]"
            for line in raw_text.strip().split("\n"):
                clean = line.strip()
                match = re.match(r"^\d+\.\s*\[?([^:\]]+)\]?\s*:\s*(.+)$", clean)
                if match:
                    label = match.group(1).strip().strip("[]'\"")
                    prompt = match.group(2).strip().strip("'\"")
                    if len(label) > 1 and len(prompt) > 5:
                        actions.append(
                            SuggestedAction(
                                label=label,
                                suggested_prompt=prompt,
                                conversational_prompt=prompt,
                                action_type=ActionType.PROMPT_SUGGESTION,
                                confidence=0.85
                            )
                        )
                elif clean.startswith(("-", "*", "•")) and ":" in clean:
                    parts = clean.lstrip("-*• ").split(":", 1)
                    label = parts[0].strip().strip("[]'\"")
                    prompt = parts[1].strip().strip("'\"")
                    if len(label) > 1 and len(prompt) > 5:
                        actions.append(
                            SuggestedAction(
                                label=label,
                                suggested_prompt=prompt,
                                conversational_prompt=prompt,
                                action_type=ActionType.PROMPT_SUGGESTION,
                                confidence=0.85
                            )
                        )
        except Exception as e:
            logger.warning(f"[ACTION PROPOSER] Generative fallback error: {e}")

        return actions
