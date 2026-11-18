"use client";
import { useCallback, useEffect, useRef, useState } from "react";
import { api, getToken, wsUrl } from "@/lib/api";
import type { Bid, Tender } from "@/lib/types";

export function useBid(tenderId: string) {
  const [tender, setTender] = useState<Tender | null>(null);
  const [bid, setBid] = useState<Bid | null>(null);
  const [live, setLive] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const bidId = bid?.id;

  const refresh = useCallback(async () => {
    try {
      let r = await api<{ tender: Tender; bid: Bid | null }>(`/api/tenders/${tenderId}`);
      if (!r.bid) r = await api<{ tender: Tender; bid: Bid }>(`/api/tenders/${tenderId}/bid`, { method: "POST" });
      setTender(r.tender);
      setBid(r.bid);
    } catch (e) {
      setError((e as Error).message);
    }
  }, [tenderId]);

  useEffect(() => { refresh(); }, [refresh]);

  // WebSocket Action Drawer feed with reconnect; a slow poll is the safety net.
  const retry = useRef(0);
  useEffect(() => {
    if (!bidId) return;
    let ws: WebSocket | null = null;
    let closed = false;
    let timer: ReturnType<typeof setTimeout>;
    const connect = () => {
      ws = new WebSocket(wsUrl(`/ws/bids/${bidId}`));
      ws.onopen = () => { ws?.send(JSON.stringify({ token: getToken() })); retry.current = 0; setLive(true); };
      ws.onmessage = (ev) => {
        const msg = JSON.parse(ev.data);
        if (msg.type === "bid.blockers") {
          setBid((b) => (b ? { ...b, ...msg.payload, id: b.id } : b));
        } else {
          refresh();
        }
      };
      ws.onclose = () => {
        setLive(false);
        if (!closed) timer = setTimeout(connect, Math.min(15000, 1000 * 2 ** retry.current++));
      };
    };
    connect();
    const ping = setInterval(() => ws?.readyState === 1 && ws.send("ping"), 25000);
    const poll = setInterval(refresh, 20000);
    return () => { closed = true; clearTimeout(timer); clearInterval(ping); clearInterval(poll); ws?.close(); };
  }, [bidId, refresh]);

  return { tender, bid, setBid, setTender, refresh, live, error };
}
