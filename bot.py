#!/usr/bin/env python3
import os, json, re, asyncio, datetime, hashlib, logging
from pathlib import Path
from duckduckgo_search import DDGS
from groq import Groq
from telegram import Update
from telegram.ext import Application, MessageHandler, CommandHandler, filters, ContextTypes

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("nexus")
BASE = Path(os.environ.get("DATA_DIR", "/tmp/nexus"))
BASE.mkdir(parents=True, exist_ok=True)
SKILLS_FILE = BASE / "skills.json"
MEMORY_FILE = BASE / "memory.json"

def jload(p, d):
    try: return json.loads(p.read_text()) if p.exists() else d
    except: return d

def jsave(p, d):
    p.write_text(json.dumps(d, indent=2, ensure_ascii=False))

class SkillEngine:
    def __init__(self):
        self.skills = jload(SKILLS_FILE, {})
    def save(self): jsave(SKILLS_FILE, self.skills)
    def _id(self, name): return hashlib.md5(name.lower().encode()).hexdigest()[:8]
    def learn(self, name, desc, tags):
        sid = self._id(name)
        if sid in self.skills:
            self.skills[sid]["uses"] += 1
        else:
            self.skills[sid] = {"name": name, "desc": desc, "tags": tags, "uses": 1, "created": datetime.datetime.now().isoformat()}
        self.save()
    def find(self, query):
        q = query.lower()
        out = []
        for s in self.skills.values():
            score = sum(3 for t in s["tags"] if t in q) + sum(1 for w in s["name"].lower().split() if w in q)
            if score: out.append((score, s))
        return [s for _, s in sorted(out, reverse=True)[:3]]
    def list_md(self):
        if not self.skills: return "Noch keine Skills."
        lines = [f"🧠 {len(self.skills)} Skills:\n"]
        for s in sorted(self.skills.values(), key=lambda x: x["uses"], reverse=True)[:20]:
            lines.append(f"• {s['name']} — {s['uses']}x")
        return "\n".join(lines)

class MemoryEngine:
    def __init__(self):
        self.data = jload(MEMORY_FILE, {"facts": []})
    def save(self): jsave(MEMORY_FILE, self.data)
    def add(self, fact):
        if fact and fact not in self.data["facts"]:
            self.data["facts"].append(fact)
            if len(self.data["facts"]) > 80: self.data["facts"].pop(0)
            self.save()
    def ctx(self):
        facts = self.data["facts"][-8:]
        return "Kontext: " + "; ".join(facts) if facts else ""
    def summary(self):
        facts = self.data["facts"]
        if not facts: return "Noch nichts gespeichert."
        return "Gedaechtnis:\n" + "\n".join(f"• {f}" for f in facts[-15:])

class NexusAgent:
    def __init__(self):
        self.groq = Groq(api_key=os.environ["GROQ_API_KEY"])
        self.skills = SkillEngine()
        self.memory = MemoryEngine()
        self.model = "llama-3.3-70b-versatile"
    def web_search(self, query):
        try:
            with DDGS() as d:
                results = list(d.text(query, max_results=4))
            return "\n".join(f"• {r['title']}: {r['body'][:200]}" for r in results)
        except: return ""
    def extract_facts(self, text):
        facts = []
        for p in [r"ich heisse \w+", r"ich bin \w+", r"ich mag .{5,40}", r"ich arbeite .{5,40}"]:
            m = re.search(p, text.lower())
            if m: facts.append(m.group(0)[:80])
        return facts
    def think(self, msg, search, skills, mem_ctx):
        skill_ctx = ("Skills: " + ", ".join(s["name"] for s in skills)) if skills else ""
        system = f"""Du bist HandyBot — intelligenter selbstlernender KI-Agent.
Deutsch, casual, du-Form. Direkt und hilfreich.
{mem_ctx}
{skill_ctx}
Web: {search if search else 'Keine Daten.'}
Max 3 kurze Absaetze."""
        resp = self.groq.chat.completions.create(
            model=self.model,
            messages=[{"role": "system", "content": system}, {"role": "user", "content": msg}],
            max_tokens=700, temperature=0.7)
        return resp.choices[0].message.content.strip()
    async def auto_learn(self, query, response):
        try:
            check = self.groq.chat.completions.create(
                model="llama-3.1-8b-instant",
                messages=[{"role": "user", "content": f"Frage: {query[:100]}\nAntwort: {response[:100]}\nNur JSON: {{\"learn\": true/false, \"name\": \"max 3 woerter\", \"desc\": \"kurz\", \"tags\": [\"tag1\",\"tag2\"]}}"}],
                max_tokens=100, temperature=0.1)
            raw = re.sub(r"```json|```", "", check.choices[0].message.content).strip()
            data = json.loads(raw)
            if data.get("learn") and data.get("name"):
                self.skills.learn(data["name"], data.get("desc",""), data.get("tags",[]))
        except: pass
    async def process(self, msg):
        search = self.web_search(msg)
        skills = self.skills.find(msg)
        mem_ctx = self.memory.ctx()
        response = self.think(msg, search, skills, mem_ctx)
        for f in self.extract_facts(msg): self.memory.add(f)
        await self.auto_learn(msg, response)
        return response

agent = NexusAgent()

async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🤖 HandyBot online!\n\nSelbstlernender KI-Agent mit Web-Suche.\nEinfach schreiben!\n\n/skills — Gelernte Skills\n/memory — Gedaechtnis\n/reset — Reset")

async def cmd_skills(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(agent.skills.list_md())

async def cmd_memory(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(agent.memory.summary())

async def cmd_reset(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    agent.memory.data = {"facts": []}
    agent.memory.save()
    await update.message.reply_text("Gedaechtnis geloescht.")

async def handle_msg(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.message.text: return
    msg = update.message.text.strip()
    if not msg: return
    await ctx.bot.send_chat_action(update.effective_chat.id, "typing")
    try:
        response = await agent.process(msg)
        if len(response) > 4000: response = response[:3990] + "..."
        await update.message.reply_text(response)
    except Exception as e:
        await update.message.reply_text(f"Fehler: {str(e)[:200]}")

def main():
    app = Application.builder().token(os.environ["TELEGRAM_BOT_TOKEN"]).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("skills", cmd_skills))
    app.add_handler(CommandHandler("memory", cmd_memory))
    app.add_handler(CommandHandler("reset", cmd_reset))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_msg))
    log.info("HandyBot gestartet")
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
