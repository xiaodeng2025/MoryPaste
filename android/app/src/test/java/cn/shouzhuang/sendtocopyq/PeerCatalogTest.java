package cn.shouzhuang.sendtocopyq;

import org.junit.Test;

import java.util.Arrays;
import java.util.Collections;
import java.util.List;
import java.util.Set;

import static org.junit.Assert.assertEquals;
import static org.junit.Assert.assertNotEquals;
import static org.junit.Assert.assertNull;

public class PeerCatalogTest {
    @Test
    public void newPairingAppendsWithoutReplacingExistingComputer() {
        PairedComputer first = peer("one", "one", "192.168.3.53", 10L, 100L);
        PairedComputer second = peer("two", "two", "192.168.3.54", 20L, 0L);
        assertEquals(Arrays.asList(first, second), PeerCatalog.INSTANCE.upsert(Collections.singletonList(first), second));
    }

    @Test
    public void updatingSameComputerPreservesPairingAndConnectionHistory() {
        PairedComputer original = peer("one", "Old", "192.168.3.10", 10L, 100L);
        PairedComputer refreshed = peer("one", "New", "192.168.3.20", 999L, 999L);
        PairedComputer result = PeerCatalog.INSTANCE.upsert(Collections.singletonList(original), refreshed).get(0);
        assertEquals("New", result.getName());
        assertEquals("192.168.3.20", result.getHost());
        assertEquals(10L, result.getPairedAt());
        assertEquals(100L, result.getLastConnectedAt());
    }

    @Test
    public void removingOneComputerKeepsOthers() {
        PairedComputer first = peer("one", "one", "192.168.3.53", 1L, 0L);
        PairedComputer second = peer("two", "two", "192.168.3.54", 2L, 0L);
        List<PairedComputer> remaining = PeerCatalog.INSTANCE.remove(Arrays.asList(first, second), first.getComputerId());
        assertEquals(Collections.singletonList(second), remaining);
    }

    @Test
    public void reachableComputersAppearFirst() {
        PairedComputer first = peer("one", "one", "192.168.3.53", 1L, 100L);
        PairedComputer second = peer("two", "two", "192.168.3.54", 2L, 50L);
        List<PairedComputer> ordered = PeerCatalog.INSTANCE.ordered(Arrays.asList(first, second), Set.of(second.getComputerId()));
        assertEquals(Arrays.asList(second, first), ordered);
    }

    @Test
    public void recentTargetRequiresSameWifiFreshResultAndUnchangedPairings() {
        PairedComputer home = peer("home", "Home", "192.168.1.2", 1L, 0L);
        PairedComputer office = peer("office", "Office", "192.168.3.53", 2L, 20L);
        List<PairedComputer> peers = List.of(home, office);
        RecentShareTarget recent = new RecentShareTarget(120L, Set.of(home.getComputerId(), office.getComputerId()),
                office.getComputerId(), "192.168.3.27", 47831, 100L);
        assertEquals("192.168.3.27", recent.peerFor(120L, peers, 200L).getHost());
        assertNull(recent.peerFor(121L, peers, 200L));
        assertNull(recent.peerFor(null, peers, 200L));
        assertNull(recent.peerFor(120L, List.of(office), 200L));
        assertNull(recent.peerFor(120L, peers, 30_101L));
    }

    @Test
    public void legacyComputerIdIsDeterministicAndCaseInsensitive() {
        String fingerprint = "ab".repeat(32);
        String id = PeerCatalog.INSTANCE.legacyComputerId(fingerprint);
        assertEquals(id, PeerCatalog.INSTANCE.legacyComputerId(fingerprint.toUpperCase()));
        assertNotEquals(id, PeerCatalog.INSTANCE.legacyComputerId("cd".repeat(32)));
        assertEquals(37, id.length());
    }

    private static PairedComputer peer(String suffix, String name, String host, long pairedAt, long lastConnectedAt) {
        String id = "stpc-" + String.format("%32s", suffix).replace(' ', '0');
        byte[] secret = new byte[32];
        Arrays.fill(secret, (byte) suffix.hashCode());
        return new PairedComputer(id, name, host, 47831, "ab".repeat(32), suffix, secret, pairedAt, lastConnectedAt);
    }
}
