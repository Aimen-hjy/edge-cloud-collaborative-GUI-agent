/**
 * agent_server.cpp
 * ================
 *
 * OpenAI-compatible HTTP server wrapping MNN LLM for on-device Agent use.
 *
 * Architecture:
 *   Python Agent (ReAct loop) --HTTP--> agent_server (this binary) --C++ API--> MNN Llm
 *
 * Build requirements:
 *   - cpp-httplib  (header-only, download from github.com/yhirose/cpp-httplib)
 *   - nlohmann/json (header-only, download from github.com/nlohmann/json)
 *   - MNN built with -DMNN_BUILD_LLM=ON
 *
 * Usage:
 *   agent_server -c model/config.json -p 9090 -H 0.0.0.0
 *
 * HTTP API:
 *   GET  /                      -> Server info
 *   GET  /health                -> Health check
 *   POST /reset                 -> Clear KV cache
 *   POST /v1/chat/completions   -> OpenAI-compatible chat (the core endpoint)
 *   OPTIONS /v1/chat/completions -> CORS preflight
 */

#include <algorithm>
#include <atomic>
#include <chrono>
#include <csignal>
#include <cstring>
#include <filesystem>
#include <iostream>
#include <memory>
#include <mutex>
#include <sstream>
#include <string>
#include <thread>
#include <vector>

// ── Third-party headers ──────────────────────────────
// Use MNN's bundled cpp-httplib to keep one ABI-compatible implementation
// in the final static binary.
#include "../src/httplib.h"

// nlohmann/json: single-header JSON library
// Download: https://github.com/nlohmann/json/blob/develop/single_include/nlohmann/json.hpp
#include "json.hpp"

// ── MNN headers ──────────────────────────────────────
#include "llm/llm.hpp"
#include <MNN/expr/ExecutorScope.hpp>

using json = nlohmann::json;
using namespace MNN::Transformer;

// ====================================================================
// Utf8StreamProcessor — Assembles complete UTF-8 characters from
// partial byte sequences. LLM output can split multi-byte chars across
// multiple xsputn() calls; this ensures we only emit complete chars.
// ====================================================================
class Utf8StreamProcessor {
public:
    using Callback = std::function<void(const std::string& utf8_char, bool finished)>;

    explicit Utf8StreamProcessor(Callback cb) : callback_(std::move(cb)) {}

    void processBytes(const char* data, size_t len) {
        for (size_t i = 0; i < len; ++i) {
            buffer_.push_back(data[i]);
            if (isComplete()) {
                std::string complete(buffer_.begin(), buffer_.end());
                buffer_.clear();
                callback_(complete, false);
            }
        }
    }

    void flush() {
        if (!buffer_.empty()) {
            std::string remaining(buffer_.begin(), buffer_.end());
            buffer_.clear();
            callback_(remaining, true);
        }
    }

private:
    bool isComplete() const {
        if (buffer_.empty()) return false;
        unsigned char c = static_cast<unsigned char>(buffer_[0]);
        size_t expected = 1;
        if ((c & 0x80u) == 0x00u)       expected = 1;  // 0xxxxxxx
        else if ((c & 0xE0u) == 0xC0u)  expected = 2;  // 110xxxxx
        else if ((c & 0xF0u) == 0xE0u)  expected = 3;  // 1110xxxx
        else if ((c & 0xF8u) == 0xF0u)  expected = 4;  // 11110xxx
        return buffer_.size() >= expected;
    }

    std::vector<char> buffer_;
    Callback callback_;
};

// ====================================================================
// LlmStreamBuffer — Custom std::streambuf that intercepts LLM output
// written to an ostream, feeding it through Utf8StreamProcessor.
// ====================================================================
class LlmStreamBuffer : public std::streambuf {
public:
    using CharCallback = std::function<void(const std::string& utf8_char, bool eop)>;

    LlmStreamBuffer(const std::string& eop_marker,
                    size_t max_buffer,
                    CharCallback on_char,
                    std::function<void(const std::string&)> on_result)
        : eop_marker_(eop_marker)
        , max_buffer_(max_buffer)
        , on_char_(std::move(on_char))
        , on_result_(std::move(on_result))
    {
        utf8_proc_ = std::make_unique<Utf8StreamProcessor>(
            [this](const std::string& ch, bool /*finished*/) {
                onUtf8Char(ch);
            });
    }

protected:
    // Called by ostream when writing bytes
    std::streamsize xsputn(const char* s, std::streamsize n) override {
        if (n <= 0) return 0;

        {
            std::lock_guard<std::mutex> lk(mutex_);
            utf8_proc_->processBytes(s, static_cast<size_t>(n));
        }
        return n;
    }

    int overflow(int c) override {
        if (c != EOF) {
            char ch = static_cast<char>(c);
            xsputn(&ch, 1);
        }
        return 0;
    }

private:
    void onUtf8Char(const std::string& ch) {
        // Accumulate characters for EOP detection
        raw_buffer_ += ch;
        eop_window_ += ch;

        // Slide the EOP detection window
        if (eop_window_.size() > eop_marker_.size()) {
            eop_window_ = eop_window_.substr(
                eop_window_.size() - eop_marker_.size());
        }

        // Check for EOP marker
        bool is_eop = (eop_window_.size() >= eop_marker_.size() &&
                       eop_window_.find(eop_marker_) != std::string::npos);

        if (on_char_) {
            on_char_(ch, is_eop);
        }

        if (is_eop) {
            // Strip EOP marker and fire on_result
            std::string result;
            {
                auto pos = raw_buffer_.rfind(eop_marker_);
                if (pos != std::string::npos) {
                    result = raw_buffer_.substr(0, pos);
                } else {
                    result = raw_buffer_;
                }
                raw_buffer_.clear();
                eop_window_.clear();
            }
            if (on_result_) {
                on_result_(result);
            }
        }

        // Safety: prevent unbounded buffer growth
        if (raw_buffer_.size() > max_buffer_) {
            raw_buffer_ = raw_buffer_.substr(raw_buffer_.size() - max_buffer_ / 2);
        }
    }

    std::string eop_marker_;
    size_t max_buffer_;
    CharCallback on_char_;
    std::function<void(const std::string&)> on_result_;
    std::unique_ptr<Utf8StreamProcessor> utf8_proc_;
    std::string raw_buffer_;
    std::string eop_window_;
    std::mutex mutex_;
};

// ====================================================================
// AgentServer — Wraps one MNN Llm instance and serves HTTP
// ====================================================================
class AgentServer {
public:
    AgentServer(std::unique_ptr<Llm> llm, const std::string& host, int port,
                const std::string& model_name = "unknown")
        : llm_(std::move(llm)), host_(host), port_(port), model_name_(model_name) {}

    void Start() {
        // Single-thread: MNN Llm is NOT thread-safe
        svr_.new_task_queue = [] { return new httplib::ThreadPool(1); };

        SetupRoutes();
        SetupCORS();

        std::cout << "[agent_server] Listening on " << host_ << ":" << port_ << std::endl;
        svr_.listen(host_.c_str(), port_);
    }

    void Stop() {
        svr_.stop();
    }

private:
    void SetupRoutes() {
        // GET /
        svr_.Get("/", [this](const httplib::Request&, httplib::Response& res) {
            json info;
            info["service"] = "mnn-agent-server";
            info["version"] = "1.0.0";
            info["ready"] = (llm_->getContext()->status != LlmStatus::NOT_LOADED);
            res.set_content(info.dump(), "application/json");
        });

        // GET /health
        svr_.Get("/health", [this](const httplib::Request&, httplib::Response& res) {
            json h;
            h["status"] = "ok";
            h["model"] = model_name_;
            h["backend"] = "cpu";
            res.set_content(h.dump(), "application/json");
        });

        // POST /reset
        svr_.Post("/reset", [this](const httplib::Request&, httplib::Response& res) {
            std::lock_guard<std::mutex> lk(llm_mutex_);
            llm_->reset();
            json r;
            r["status"] = "ok";
            r["message"] = "KV cache cleared";
            res.set_content(r.dump(), "application/json");
        });

        // POST /v1/chat/completions — the core endpoint
        svr_.Post("/v1/chat/completions",
                  [this](const httplib::Request& req, httplib::Response& res) {
            HandleChatCompletion(req, res);
        });

        // OPTIONS /v1/chat/completions — CORS preflight
        svr_.Options("/v1/chat/completions",
                     [](const httplib::Request&, httplib::Response& res) {
            res.set_header("Access-Control-Allow-Methods", "POST, OPTIONS");
            res.set_header("Access-Control-Allow-Headers", "Content-Type");
            res.status = 204;
        });
    }

    void SetupCORS() {
        svr_.set_pre_routing_handler([](const httplib::Request&, httplib::Response& res) {
            res.set_header("Access-Control-Allow-Origin", "*");
            return httplib::Server::HandlerResponse::Unhandled;
        });
    }

    // ── /v1/chat/completions handler ────────────────

    void HandleChatCompletion(const httplib::Request& req, httplib::Response& res) {
        // Parse request body
        json body;
        try {
            body = json::parse(req.body);
        } catch (const json::parse_error& e) {
            res.status = 400;
            res.set_content(R"({"error":"Invalid JSON"})", "application/json");
            return;
        }

        // Extract parameters
        auto messages_it = body.find("messages");
        if (messages_it == body.end() || !messages_it->is_array()) {
            res.status = 400;
            res.set_content(R"({"error":"'messages' array is required"})", "application/json");
            return;
        }

        bool stream = body.value("stream", false);
        int max_tokens = body.value("max_tokens", 2048);

        // Build ChatMessages from OpenAI JSON
        ChatMessages chat_msgs;
        std::string system_prompt;
        try {
            BuildChatMessages(*messages_it, body.value("tools", json::array()),
                              chat_msgs, system_prompt);
        } catch (const std::exception& e) {
            res.status = 400;
            json err;
            err["error"] = std::string("Bad message format: ") + e.what();
            res.set_content(err.dump(), "application/json");
            return;
        }

        // Run inference (serialized by mutex)
        std::string generated;
        LlmStatus status;
        {
            std::lock_guard<std::mutex> lk(llm_mutex_);

            // Apply runtime temperature. MNN snapshots sampler params when the
            // sampler is built in load(), so a runtime change needs the sampler
            // re-created. Only rebuild when the value actually changes to avoid
            // resetting the RNG on every request.
            if (body.contains("temperature") && body["temperature"].is_number()) {
                float t = body["temperature"].get<float>();
                if (t != applied_temperature_) {
                    json cfg;
                    if (t <= 0.0f) {
                        // MNN's temperature sampler divides by temperature, so
                        // 0 is invalid. temperature<=0 means deterministic:
                        // switch to the greedy sampler (argmax).
                        cfg["sampler_type"] = "greedy";
                    } else {
                        cfg["temperature"] = t;
                        cfg["sampler_type"] = "mixed";
                    }
                    llm_->set_config(cfg.dump());
                    llm_->reloadSampler();
                    applied_temperature_ = t;
                }
            }

            if (stream) {
                generated = AnswerStreaming(chat_msgs, max_tokens, res);
                // AnswerStreaming already wrote the response
                return;
            } else {
                generated = Answer(chat_msgs, max_tokens);
            }

            status = llm_->getContext()->status;
        }

        // Build and send response
        json resp = BuildCompletionResponse(generated, status);
        res.set_content(resp.dump(), "application/json");
    }

    // ── Non-streaming answer ────────────────────────

    std::string Answer(const ChatMessages& messages, int max_tokens) {
        std::string result;
        std::mutex result_mutex;
        bool done = false;

        // Create stream buffer that intercepts output
        LlmStreamBuffer stream_buf(
            "<eop>",             // EOP marker (MNN outputs this on EOS)
            10 * 1024 * 1024,    // max buffer 10MB
            // on_char: per-char callback (not used in non-streaming)
            [](const std::string&, bool) {},
            // on_result: called when EOP detected
            [&](const std::string& text) {
                std::lock_guard<std::mutex> lk(result_mutex);
                result = text;
                done = true;
            });

        std::ostream output_stream(&stream_buf);

        // Run LLM — writes to output_stream, EOP triggers on_result
        llm_->response(messages, &output_stream, "<eop>", max_tokens);

        // Flush remaining bytes (if EOP wasn't detected)
        if (!done) {
            const auto* ctx = llm_->getContext();
            if (ctx && !ctx->generate_str.empty()) {
                // Strip trailing EOP if present
                std::string gen = ctx->generate_str;
                auto pos = gen.rfind("<eop>");
                if (pos != std::string::npos) {
                    gen = gen.substr(0, pos);
                }
                return gen;
            }
        }

        return result;
    }

    // ── Streaming (SSE) answer ──────────────────────

    std::string AnswerStreaming(const ChatMessages& messages, int max_tokens,
                                httplib::Response& res) {
        res.set_header("Content-Type", "text/event-stream");
        res.set_header("Cache-Control", "no-cache");
        res.set_header("Connection", "keep-alive");

        // We need to build the SSE chunks as the LLM generates.
        // Since httplib expects us to set content before returning,
        // we use a chunked transfer encoding via a custom content provider.

        auto content_provider = [this, messages, max_tokens](
            size_t /*offset*/, httplib::DataSink& sink) -> bool {

            std::string buffer;
            std::mutex buf_mutex;
            bool generation_done = false;
            std::string final_content;

            LlmStreamBuffer stream_buf(
                "<eop>",
                10 * 1024 * 1024,
                // on_char: emit SSE chunks for each complete UTF-8 character
                [&](const std::string& ch, bool /*is_eop*/) {
                    std::lock_guard<std::mutex> lk(buf_mutex);
                    buffer += ch;
                },
                // on_result: EOP detected, finalize
                [&](const std::string& text) {
                    std::lock_guard<std::mutex> lk(buf_mutex);
                    final_content = text;
                    generation_done = true;
                });

            // Run generation in a separate thread
            std::thread gen_thread([&]() {
                std::ostream output_stream(&stream_buf);
                llm_->response(messages, &output_stream, "<eop>", max_tokens);
            });

            // Stream chunks to client
            size_t sent_pos = 0;
            auto last_send = std::chrono::steady_clock::now();

            while (!generation_done) {
                std::string chunk;
                {
                    std::lock_guard<std::mutex> lk(buf_mutex);
                    if (buffer.size() > sent_pos) {
                        chunk = buffer.substr(sent_pos);
                        sent_pos = buffer.size();
                    }
                }

                if (!chunk.empty()) {
                    // Escape for JSON
                    std::string escaped;
                    for (char c : chunk) {
                        if (c == '"') escaped += "\\\"";
                        else if (c == '\\') escaped += "\\\\";
                        else if (c == '\n') escaped += "\\n";
                        else if (c == '\r') escaped += "\\r";
                        else if (c == '\t') escaped += "\\t";
                        else escaped += c;
                    }

                    std::string sse = "data: {\"choices\":[{\"delta\":{\"content\":\""
                                    + escaped + "\"}}]}\n\n";
                    if (!sink.write(sse.data(), sse.size())) {
                        gen_thread.detach(); // can't join, just abandon
                        return false;
                    }
                    last_send = std::chrono::steady_clock::now();
                } else {
                    std::this_thread::sleep_for(std::chrono::milliseconds(10));
                }

                // Timeout safety
                auto elapsed = std::chrono::steady_clock::now() - last_send;
                if (elapsed > std::chrono::seconds(120)) {
                    gen_thread.detach();
                    return false;
                }
            }

            gen_thread.join();

            // Send final [DONE]
            sink.write("data: [DONE]\n\n", 15);

            return true;
        };

        res.set_chunked_content_provider("text/event-stream", content_provider,
                                         [](bool /*success*/) { /* cleanup */ });
        return "";
    }

    // ── BuildCompletionResponse ──────────────────────

    json BuildCompletionResponse(const std::string& text, LlmStatus status) {
        json resp;
        resp["id"] = "chatcmpl-local";
        resp["object"] = "chat.completion";
        resp["created"] = std::chrono::duration_cast<std::chrono::seconds>(
            std::chrono::system_clock::now().time_since_epoch()).count();
        resp["model"] = model_name_;

        json choice;
        choice["index"] = 0;
        choice["message"]["role"] = "assistant";
        choice["message"]["content"] = text;

        // Try to parse tool_calls from the text (server returns them structured)
        auto tool_calls = TryParseToolCalls(text);
        if (!tool_calls.empty()) {
            choice["message"]["tool_calls"] = tool_calls;
        }

        switch (status) {
            case LlmStatus::NORMAL_FINISHED:
                choice["finish_reason"] = "stop"; break;
            case LlmStatus::MAX_TOKENS_FINISHED:
                choice["finish_reason"] = "length"; break;
            default:
                choice["finish_reason"] = "stop"; break;
        }

        resp["choices"] = json::array({choice});

        json usage;
        usage["prompt_tokens"] = 0;
        usage["completion_tokens"] = 0;
        usage["total_tokens"] = 0;
        resp["usage"] = usage;

        return resp;
    }

    // ── TryParseToolCalls ────────────────────────────

    /**
     * Extract <tool_call> blocks from generated text and return them
     * in OpenAI tool_calls format. This lets the Python agent receive
     * structured tool calls instead of having to parse raw text.
     */
    json TryParseToolCalls(const std::string& text) {
        json result = json::array();

        // Find all <tool_call>...</tool_call> blocks
        size_t pos = 0;
        int call_idx = 0;
        while (true) {
            auto start = text.find("<tool_call>", pos);
            if (start == std::string::npos) break;
            start += 11; // length of "<tool_call>"

            auto end = text.find("</tool_call>", start);
            if (end == std::string::npos) break;

            std::string json_str = text.substr(start, end - start);
            // Trim whitespace
            size_t first = json_str.find_first_not_of(" \t\n\r");
            size_t last = json_str.find_last_not_of(" \t\n\r");
            if (first != std::string::npos && last != std::string::npos) {
                json_str = json_str.substr(first, last - first + 1);
            }

            try {
                json tc = json::parse(json_str);
                std::string name = tc.value("name", "");
                json arguments = tc.value("arguments", json::object());

                json openai_tc;
                openai_tc["id"] = "call_" + std::to_string(call_idx++);
                openai_tc["type"] = "function";
                openai_tc["function"]["name"] = name;
                openai_tc["function"]["arguments"] = arguments.is_string()
                    ? arguments.get<std::string>()
                    : arguments.dump();
                result.push_back(openai_tc);
            } catch (const json::parse_error&) {
                // Ignore malformed tool calls
            }

            pos = end + 12; // length of "</tool_call>"
        }

        return result;
    }

    // ── BuildChatMessages ────────────────────────────

    /**
     * Convert OpenAI JSON messages → MNN ChatMessages.
     *
     * Mapping (per arc.md):
     *   role:"system"      → ("system", content + tool_defs)
     *   role:"user"        → ("user",   content)
     *   role:"assistant"   → ("assistant", content)  or ("json", full_json) if has tool_calls
     *   role:"tool"        → ("json",    tool_result_json)
     */
    void BuildChatMessages(const json& messages_json,
                           const json& tools_json,
                           ChatMessages& out,
                           std::string& system_prompt_out) {
        for (const auto& msg : messages_json) {
            std::string role = msg.value("role", "");
            std::string content = msg.value("content", "");

            if (role == "system") {
                system_prompt_out = content;
                // Append tool definitions
                std::string full_system = content;
                if (!tools_json.empty()) {
                    full_system += "\n\n## Available Tools\n\n";
                    for (const auto& tool : tools_json) {
                        const auto& func = tool.value("function",
                            tool); // handle both {"type":"function","function":{...}} and raw
                        std::string name = func.value("name", "");
                        std::string desc = func.value("description", "");
                        full_system += "- **" + name + "**: " + desc + "\n";
                        // Inject the argument JSON-schema so the model sees
                        // parameter names/types (previously only name+desc were
                        // injected, hiding argument names from the model).
                        if (func.contains("parameters") && func["parameters"].is_object()) {
                            const auto& params = func["parameters"];
                            if (params.contains("properties") && !params["properties"].empty()) {
                                full_system += "    parameters: " + params.dump() + "\n";
                            }
                        }
                    }
                    full_system += "\n## Tool Call Format\n\n"
                        "To use a tool, output exactly:\n"
                        "<tool_call>\n"
                        R"({"name": "tool_name", "arguments": {"param": "value"}})"
                        "\n</tool_call>\n"
                        "The system will execute the tool and return the result. "
                        "Continue with another tool call or give the final answer.\n";
                }
                out.emplace_back("system", full_system);

            } else if (role == "user") {
                out.emplace_back("user", content);

            } else if (role == "assistant") {
                // Check for tool_calls — if present, serialize full message as JSON
                if (msg.contains("tool_calls") && !msg["tool_calls"].is_null()) {
                    out.emplace_back("json", msg.dump());
                } else {
                    out.emplace_back("assistant", content);
                }

            } else if (role == "tool") {
                // Serialize tool result as JSON for jinja template
                json tool_msg;
                tool_msg["role"] = "tool";
                tool_msg["tool_call_id"] = msg.value("tool_call_id", "");
                tool_msg["content"] = content;
                out.emplace_back("json", tool_msg.dump());

            } else {
                // Unknown role — pass through as user
                out.emplace_back("user", content);
            }
        }
    }

    // ── Members ──────────────────────────────────────

    std::unique_ptr<Llm> llm_;
    std::string host_;
    int port_;
    std::string model_name_;
    httplib::Server svr_;
    std::mutex llm_mutex_;
    float applied_temperature_ = -1.0f;  // last temperature applied to the sampler
};

// ====================================================================
// CLI & Main
// ====================================================================

static std::atomic<bool> g_running{true};

static void PrintUsage(const char* prog) {
    std::cerr << "Usage: " << prog << " -c <config.json> [-p <port>] [-H <host>] [-t <threads>]\n"
              << "\n"
              << "Options:\n"
              << "  -c, --config <path>    MNN model config.json (required)\n"
              << "  -p, --port <n>         HTTP listen port (default: 9090)\n"
              << "  -H, --host <addr>      HTTP listen address (default: 0.0.0.0)\n"
              << "  -t, --threads <n>      CPU threads (default: 4)\n"
              << "  -h, --help             Show this help\n"
              << std::endl;
}

int main(int argc, char** argv) {
    std::string config_path;
    std::string host = "0.0.0.0";
    int port = 9090;
    int threads = 4;

    // Parse CLI
    for (int i = 1; i < argc; ++i) {
        std::string arg = argv[i];
        if ((arg == "-c" || arg == "--config") && i + 1 < argc) {
            config_path = argv[++i];
        } else if ((arg == "-p" || arg == "--port") && i + 1 < argc) {
            port = std::stoi(argv[++i]);
        } else if ((arg == "-H" || arg == "--host") && i + 1 < argc) {
            host = argv[++i];
        } else if ((arg == "-t" || arg == "--threads") && i + 1 < argc) {
            threads = std::stoi(argv[++i]);
        } else if (arg == "-h" || arg == "--help") {
            PrintUsage(argv[0]);
            return 0;
        } else {
            std::cerr << "Unknown option: " << arg << "\n";
            PrintUsage(argv[0]);
            return 1;
        }
    }

    if (config_path.empty()) {
        std::cerr << "Error: -c <config.json> is required\n";
        PrintUsage(argv[0]);
        return 1;
    }

    // ── Initialize MNN LLM ──────────────────────────

    // Extract model name from the current working directory
    // (config.json is at model/config.json, project dir is cwd)
    std::string model_name = std::filesystem::current_path().filename().string();
    // Strip common prefixes/suffixes for a cleaner display name
    static const std::string PREFIX = "mnn-";
    if (model_name.compare(0, PREFIX.size(), PREFIX) == 0)
        model_name = model_name.substr(PREFIX.size());
    auto dash_pos = model_name.rfind("-q");
    if (dash_pos != std::string::npos) model_name = model_name.substr(0, dash_pos);
    if (model_name.empty()) model_name = "unknown";

    std::cout << "[agent_server] Creating LLM from: " << config_path << std::endl;

    // Create executor
    MNN::BackendConfig backendConfig;
    auto executor = MNN::Express::Executor::newExecutor(
        MNN_FORWARD_CPU, backendConfig, threads);
    MNN::Express::ExecutorScope scope(executor);

    // Create Llm
    std::unique_ptr<Llm> llm(Llm::createLLM(config_path));
    if (!llm) {
        std::cerr << "Error: Llm::createLLM returned null\n";
        return 1;
    }

    // Configure for agent use
    llm->set_config(R"({"reuse_kv":true})");       // Keep KV cache across turns
    llm->set_config(R"({"prompt_cache":true})");    // Incremental prefill
    llm->set_config(R"({"max_new_tokens":2048})");  // Allow long outputs
    llm->set_config(R"({"async":false})");          // Synchronous mode

    // Optionally disable thinking mode for Qwen3
    llm->set_config(R"({"jinja":{"context":{"enable_thinking":false}}})");

    // Load model
    std::cout << "[agent_server] Loading model..." << std::endl;
    if (!llm->load()) {
        std::cerr << "Error: Llm::load() failed\n";
        return 1;
    }

    // Warmup
    std::cout << "[agent_server] Warming up (tuning)..." << std::endl;
    llm->tuning(OP_ENCODER_NUMBER, {1, 5, 10, 20, 30, 50, 100});

    std::cout << "[agent_server] Model loaded. Starting server..." << std::endl;

    // ── Start HTTP server ───────────────────────────

    // Handle SIGINT gracefully
    std::signal(SIGINT, [](int) { g_running = false; });

    AgentServer server(std::move(llm), host, port, model_name);

    // Run server in a thread so we can handle signals
    std::thread server_thread([&server]() {
        server.Start();
    });

    while (g_running) {
        std::this_thread::sleep_for(std::chrono::milliseconds(100));
    }

    std::cout << "\n[agent_server] Shutting down..." << std::endl;
    server.Stop();
    server_thread.join();

    std::cout << "[agent_server] Done." << std::endl;
    return 0;
}
